# Дополнительная лекция 2. Go GC internals и memory allocator

> Версионная привязка: материал ориентирован на Go 1.27.x.
>
> Здесь важно различать три уровня:
>
> - **устойчивая учебная модель** — идеи, которые полезно помнить между версиями Go;
> - **публичное поведение runtime** — то, на что можно опираться при эксплуатации;
> - **детали текущей реализации** — `mcache`, `mspan`, `mcentral`, Green Tea internals и конкретные функции runtime. Они могут меняться между версиями.

Основная лекция про память отвечает на вопросы:

```text
что такое stack и heap?
почему значение escape'ится?
что делает GC?
как пользоваться pprof?
```

Эта лекция открывает следующий слой:

```text
компилятор решил:
"нужна heap allocation"

                    │
                    ▼

а что дальше физически делает runtime?
```

Нас будет интересовать вся цепочка:

```text
source code
    ↓
escape analysis
    ↓
heap allocation
    ↓
mcache / mspan / mcentral / mheap
    ↓
heap growth
    ↓
GC pacer
    ↓
concurrent marking
    ↓
write barriers
    ↓
mark workers + assists
    ↓
sweep
    ↓
scavenger
    ↓
OS
```

Главная идея лекции:

> Memory management в Go — это не отдельный «сборщик мусора». Это работающая одновременно система allocator + GC + scheduler + virtual memory ОС.

---

# Блок 1. От `new(T)` до куска памяти

## 1. Почему нельзя объяснить heap словами «runtime выделил память»

Возьмём простой код:

```go
type User struct {
    ID   int64
    Name string
}

func newUser() *User {
    u := User{
        ID:   42,
        Name: "Alice",
    }
    return &u
}
```

Компилятор видит, что адрес `u` покидает функцию.

Упрощённо:

```text
u
↓
escape analysis
↓
escapes to heap
```

На основной лекции этого было достаточно.

Теперь задаём следующий вопрос:

> Где runtime возьмёт память под `User`?

Самый наивный вариант:

```text
new(User)
↓
обращение в Linux
↓
дай мне 32 байта
```

Такой allocator был бы катастрофически дорогим.

Почему?

Потому что backend может выполнять миллионы allocations в секунду.

Если каждая allocation превращается в системный вызов:

```text
application
↓
kernel
↓
VM subsystem
↓
application
```

стоимость memory management становится огромной.

Поэтому runtime работает крупными блоками памяти и самостоятельно раздаёт из них маленькие объекты.

Ментальная модель:

```text
ОС выдаёт Go большие области памяти
               ↓
Go runtime управляет ими сам
               ↓
приложение получает маленькие объекты
```

Это первая фундаментальная идея allocator.

## 2. Масштабы памяти

Очень полезно сразу разделить уровни.

```text
объект Go
   ↓
slot внутри span
   ↓
span
   ↓
runtime pages
   ↓
большие области virtual memory
   ↓
memory mappings ОС
   ↓
physical pages
```

Это разные сущности.

Когда код делает:

```go
p := new(User)
```

мы говорим:

> «выделился объект».

Runtime внизу думает уже другими категориями:

```text
size class
span class
free bitmap
runtime page
page allocator
```

ОС вообще не знает, что существует `User`.

Для ядра это просто страницы virtual memory процесса.

## 3. Почему allocator построен иерархически

Backend выполняется параллельно.

Пусть:

```text
GOMAXPROCS = 8
```

и одновременно тысячи goroutines создают объекты.

Наивная схема:

```text
G1 ─┐
G2 ─┤
G3 ─┤
... ├── global allocator lock
Gn ─┘
```

Тогда быстрый код:

```go
x := new(RequestContext)
```

может упираться в общий mutex allocator.

Получается:

```text
CPU cores растут
↓
goroutines растут
↓
конкуренция за один allocator lock растёт
↓
масштабирование ломается
```

Поэтому Go использует многоуровневый allocator:

```text
локальный быстрый путь
        ↓
общий более дорогой путь
        ↓
глобальный page allocator
        ↓
ОС
```

В текущем runtime учебная цепочка выглядит так:

```text
P
↓
mcache
↓
mspan
↓
mcentral
↓
mheap
↓
page allocator
↓
OS
```

Но важно читать её правильно.

Это не означает:

> каждая allocation проходит через все уровни.

Наоборот.

Цель архитектуры состоит именно в том, чтобы **обычная allocation закончилась как можно выше**.

## 4. Fast path маленькой allocation

Для большинства небольших объектов идеальный путь:

```text
goroutine выполняется на P
        ↓
у P уже есть подходящий mspan
        ↓
в span есть свободный slot
        ↓
runtime забирает slot
        ↓
готово
```

То есть:

```text
P
↓
mcache
↓
cached mspan
↓
free bitmap
↓
slot
```

Без:

```text
mcentral
mheap
mmap
глобального allocator lock
```

Именно это делает частую маленькую allocation достаточно дешёвой.

Но позже мы увидим важную вещь:

> Дешёвая allocation сейчас ещё не означает дешёвый объект за весь его жизненный цикл.

## Что важно запомнить

1. ОС не обслуживает каждый `new(T)` отдельно.
2. Runtime получает память крупными блоками и сам делит её на объекты.
3. Allocator специально имеет локальный fast path.
4. Иерархия существует, чтобы редкие дорогие операции амортизировать на множество дешёвых allocations.
5. Объект Go, span, runtime page и physical page ОС — разные уровни.

---

# Блок 2. Почему allocator привязан к P

## 5. Возвращаем G / M / P

Из лекции про scheduler:

```text
G = goroutine
M = OS thread
P = scheduler resource
```

Для выполнения Go-кода `M` обычно нужен `P`.

Упрощённо:

```text
G
↓
M + P
↓
CPU
```

И вот allocator тоже привязан к `P`.

У каждого `P` есть свой allocator cache:

```text
P0 → mcache0
P1 → mcache1
P2 → mcache2
P3 → mcache3
```

Почему это хороший дизайн?

Потому что количество goroutines может быть огромным:

```text
100
10 000
1 000 000
```

А количество реально параллельно выполняемого Go-кода ограничено количеством `P`.

Если сделать allocator cache на goroutine:

```text
1 000 000 goroutines
↓
1 000 000 allocator caches
```

Получаем чудовищный объём metadata.

Если сделать один общий cache:

```text
все P
↓
один lock
```

получаем contention.

Per-P cache даёт компромисс:

```text
число caches ≈ число единиц параллельного выполнения
```

## 6. Почему именно P, а не M

Можно спросить:

> Почему allocator cache не привязан к OS thread `M`?

Потому что `M` — исполнитель, который может:

- блокироваться в syscall;
- появляться;
- исчезать;
- менять P.

`P` лучше отражает именно право выполнять Go-код.

Для runtime удобно хранить hot state рядом с P:

```text
scheduler local run queue
allocator cache
GC local work
```

Это общий архитектурный паттерн Go runtime:

```text
сначала работай локально
↓
как можно реже координируйся глобально
```

Мы увидим его ещё несколько раз.

## 7. Что такое mcache на человеческом языке

Название `mcache` легко понять неправильно.

Это не:

```text
один большой кусок свободной памяти
```

Гораздо точнее:

> `mcache` — набор подготовленных allocator resources, которые конкретный P может использовать без общего lock.

Внутри концептуально есть ссылки на spans разных классов:

```text
mcache
│
├── spanClass A → mspan
├── spanClass B → mspan
├── spanClass C → mspan
├── spanClass D → mspan
└── ...
```

Например условно:

```text
mcache P0

8-byte noscan      → span #101
16-byte scan       → span #533
24-byte noscan     → span #401
32-byte scan       → span #829
48-byte noscan     → span #711
...
```

Когда нужно выделить объект:

```text
размер + contains pointers?
↓
spanClass
↓
нужный mspan
```

## 8. Почему fast path не требует общего lock

Представим:

```text
P0 использует mcache0
P1 использует mcache1
```

В нормальной ситуации P0 не пытается одновременно выделять из allocator cache P1.

Поэтому:

```text
P-local state
↓
нет конкурентного доступа в common case
↓
нет необходимости брать глобальный lock
```

Это важный момент.

Go allocator быстрый не потому, что:

> «malloc написан очень оптимизированно».

Главная архитектурная причина:

> Частый путь специально построен так, чтобы избегать глобальной синхронизации.

---

# Блок 3. mspan — центральная единица allocator

## 9. Проблема маленьких объектов

Пусть мы постоянно создаём структуру размером около 40 байт.

```go
type Entry struct {
    A uint64
    B uint64
    C uint64
    D uint64
    E uint32
}
```

Реальные программы создают объекты сотен разных размеров. Если без системы смешивать их в памяти:

```text
13 B
840 B
39 B
5 B
2048 B
61 B
...
```

очень быстро появляется сложная fragmentation problem.

Runtime использует size classes.

## 10. Size class

Идея:

> Не поддерживать отдельный allocator для каждого возможного размера.

Вместо этого диапазоны размеров округляются до фиксированных классов.

Для маленьких размеров:

```text
1–8 B   → slot 8 B
9–16 B  → slot 16 B
17–24 B → slot 24 B
25–32 B → slot 32 B
33–48 B → slot 48 B
49–64 B → slot 64 B
65–80 B → slot 80 B
...
```

Пусть объект требует:

```text
37 B
```

Он попадает в:

```text
48 B size class
```

и получает slot:

```text
48 B
```

Внутри:

```text
37 B полезные данные
11 B внутренний waste
```

Это internal fragmentation.

## 11. Зачем платить за internal fragmentation

Потому что мы резко упрощаем allocator.

Вместо:

```text
найди мне случайный непрерывный кусок ≥37 B
```

можно делать:

```text
дай следующий свободный 48-byte slot
```

Trade-off:

```text
фиксированные size classes
↓
быстрый allocator
+
предсказуемое размещение
-
немного потерянной памяти
```

## 12. Что такое mspan

`mspan` — структура runtime, которая описывает непрерывный набор runtime pages.

Для small objects span обычно используется под один span class.

```text
mspan для 48-byte objects

┌──────┬──────┬──────┬──────┬──────┬──────┐
│ 48 B │ 48 B │ 48 B │ 48 B │ 48 B │ ...  │
└──────┴──────┴──────┴──────┴──────┴──────┘
```

Каждый прямоугольник — потенциальный object slot.

```text
mspan
≠
один объект
```

Один span обычно содержит множество slots.

## 13. Runtime page

`mheap` управляет памятью с granularity runtime page.

В Go 1.27 runtime page:

```text
8192 bytes = 8 KiB
```

Span может занимать одну или несколько таких pages:

```text
mspan
│
├── runtime page 0
├── runtime page 1
├── runtime page 2
└── runtime page 3
```

Это implementation detail Go runtime.

```text
runtime page
≠
physical page Linux
```

## 14. Пример span в цифрах

Предположим учебно:

```text
span size = 8192 B
slot size = 64 B
```

Тогда:

```text
8192 / 64 = 128 slots
```

Когда приложение делает 100 allocations по 64 байта, runtime может обслужить их из одного уже подготовленного span.

```text
100 allocations
≠
100 обращений к OS
```

## 15. allocBits

Runtime должен знать, какие slots заняты.

```text
slot:      0 1 2 3 4 5 6 7
allocBits: 1 1 0 1 1 0 0 1
```

Где:

```text
1 = allocated
0 = free
```

Нужно выделить следующий объект:

```text
найти подходящий 0
↓
зарезервировать slot
↓
вернуть address
```

## 16. freeindex

Если каждый allocation начинать с slot 0, runtime будет снова и снова пересматривать занятые места.

Поэтому span хранит информацию, откуда продолжать поиск свободного slot.

```text
slot:       0 1 2 3 4 5 6 7
allocBits:  1 1 1 1 0 0 1 0
freeindex:          ↑
```

## 17. Почему mspan важен не только allocator

Позже GC тоже будет работать с этими же spans.

```text
allocator:
какие slots заняты?

GC:
какие objects живы?

sweeper:
какие slots можно снова считать свободными?
```

`mspan` — место встречи:

```text
allocation
GC marking
sweeping
```

## Что важно запомнить

1. Size class — обмен небольшой internal fragmentation на быстрый allocator.
2. `mspan` — metadata для run runtime pages, разбитого на slots одного класса.
3. Один span содержит много объектов.
4. Allocator хранит bitmap состояния slots.
5. `mspan` используется и allocator, и GC.

---

# Блок 4. scan и noscan

## 18. Одинаковый размер ещё не означает одинаковую стоимость

```go
type A struct {
    X uint64
    Y uint64
}
```

```go
type B struct {
    X *User
    Y uint64
}
```

У `A` нет heap pointers. После того как GC установил, что A reachable, внутрь объекта можно не идти.

У `B`:

```text
X → другой heap object
```

значит GC должен проверить эту ссылку.

## 19. spanClass

Runtime различает:

```text
size class
+
scan / noscan
```

В текущем runtime `spanClass` кодирует size class и `noscan` bit.

Поэтому два объекта одного размера могут обслуживаться разными spans:

```text
32-byte noscan
32-byte scan
```

## 20. Почему `[]byte` и `[]*Node` — разные heap objects

### Вариант A

```go
buf := make([]byte, 100<<20)
```

Внутри нет pointers.

### Вариант B

```go
nodes := make([]*Node, ...)
```

Пусть backing array тоже занимает около 100 MiB.

Теперь внутри:

```text
ptr
ptr
ptr
ptr
...
```

Каждый pointer потенциально ведёт к следующему объекту графа.

```text
одинаковые heap bytes
≠
одинаковая GC work
```

## 21. Scannable heap

Heap size сам по себе недостаточен.

Нас интересует:

```text
сколько memory GC должен реально просматривать в поисках pointers
```

В `runtime/metrics` есть:

```text
/gc/scan/heap:bytes
```

Если Heap A в основном large byte buffers, а Heap B — pointer-rich graph, одинаковый объём памяти даст разную collector work.

---

# Блок 5. Что происходит при маленькой allocation

## 22. Конкретный путь

```go
u := &User{}
```

Учебный алгоритм:

```text
1. определить размер
2. определить содержит ли объект pointers
3. выбрать spanClass
4. взять mspan из mcache текущего P
5. найти free slot
6. при необходимости очистить memory
7. пометить slot allocated
8. вернуть pointer
```

Если span содержит место — готово.

## 23. Почему память надо zero'ить

Go гарантирует zero value.

Если allocator повторно использует slot, там физически могут оставаться байты прошлого объекта.

Новый object должен получить корректное zeroed состояние там, где это требуется.

Zeroing — тоже часть стоимости allocation.

## 24. Go 1.27: size-specialized allocation

Generic allocator получает:

```text
size
contains pointers?
```

и вычисляет дальнейшую ветку.

Но compiler часто уже знает всё заранее.

```go
type Pair struct {
    A uint64
    B uint64
}

p := new(Pair)
```

Здесь известны:

```text
size = 16 B
noscan
```

Go 1.27 для ряда объектов меньше 80 байт использует size-specialized allocation routines.

```text
generic path:
определи class
проверь branch
определи scan
...

specialized path:
class уже известен
↓
сразу выполняем нужную работу
```

Это деталь Go 1.27, а не гарантия языка.

---

# Блок 6. Когда локальный span закончился

## 25. mcache исчерпан

```text
P0
↓
mcache
↓
64-byte span
```

В какой-то момент:

```text
free slots = 0
```

Следующий уровень:

```text
mcentral
```

## 26. Что делает mcentral

Для каждого span class существует центральное управление spans.

```text
mcentral[64-byte noscan]

partial spans:
    span A
    span B

full spans:
    span C
    span D
```

`mcentral` не является мешком отдельных free objects. Свободные object slots находятся внутри `mspan`.

## 27. Почему mcache получает span, а не один object

Плохой вариант:

```text
lock mcentral
↓
выдать один object
↓
unlock
```

Миллионы allocations → миллионы lock/unlock.

Правильнее:

```text
редко:
mcache получает span

часто:
mcache раздаёт из него много objects
```

Это амортизация дорогой refill operation.

## 28. Contention переносится с hot path

Performance pattern:

```text
не обязательно удалить synchronization полностью
↓
нужно убрать её с каждого hot-path operation
```

---

# Блок 7. mheap и page allocator

## 29. Если mcentral тоже не может дать span

```text
mcentral
↓
mheap
```

На этом уровне runtime думает страницами:

```text
дай N contiguous runtime pages
```

## 30. Что такое mheap

Полезная модель:

> `mheap` — глобальная структура runtime, координирующая heap memory на уровне spans/pages и связанную metadata.

Для маленьких объектов:

```text
mheap
↓
span
↓
mcentral
↓
mcache
↓
object
```

## 31. Page allocator

Runtime должен найти contiguous run свободных runtime pages.

```text
used used free free free used free ...
          └───────┘
             ↑
        будущий span
```

## 32. Когда появляется ОС

Если у runtime недостаточно доступной memory:

```text
page allocator
↓
runtime OS abstraction
↓
mmap / platform mechanism
```

Поэтому маленький allocation обычно не означает syscall.

---

# Блок 8. Small, large и tiny allocations

## 33. Small objects

В текущем runtime small objects:

```text
≤ 32 KiB
```

Для них работают size classes.

## 34. Large objects

```go
buf := make([]byte, 10<<20)
```

10 MiB не обслуживаются обычным small-object size class.

```text
large object
↓
mheap / page allocator
↓
достаточное количество pages
```

Large object получает span подходящего размера.

## 35. Почему large allocations чувствительны

```text
большая allocation
↓
heapLive резко растёт
↓
GC goal приближается
```

Большие blocks также влияют на fragmentation, RSS и scavenging.

## 36. Tiny allocator

Для очень маленьких pointer-free allocations текущий runtime использует tiny allocator.

Tiny block:

```text
16 B
```

может содержать несколько tiny objects.

```text
┌─────┬──────┬──────────┬────────┐
│ 1 B │ 2 B  │   4 B    │ ...    │
└─────┴──────┴──────────┴────────┘
```

## 37. Почему только noscan

Pointer-containing tiny objects потребовали бы сложной индивидуальной GC metadata внутри совместно размещённого block.

Поэтому tiny allocator применяется к objects без pointers.

## 38. Цена tiny allocator

```text
A dead
B alive
```

может означать, что весь tiny block пока нельзя освободить.

Trade-off:

```text
немного retention
↔
меньше allocator overhead
```

## Что важно запомнить

1. Small objects обслуживаются size classes.
2. Large objects идут ближе к page allocator.
3. Tiny pointer-free objects могут делить 16-byte block.
4. Чем дальше путь уходит от mcache, тем он дороже.
5. Allocator decisions тесно связаны с GC.

---

# Блок 9. Теперь появляется GC

## 39. Allocator умеет занять slot. Но кто его освободит?

```text
A reachable
B unreachable
C reachable
D unreachable
```

Allocator знает только:

```text
slot занят
```

Чтобы переиспользовать B, runtime должен доказать:

```text
B больше недостижим
```

## 40. Reachability

GC не понимает бизнес-смысл.

Он знает graph reachability:

```text
roots
↓
A
↓
C
```

Есть путь от roots → object live.

Нет пути → garbage candidate.

## 41. Roots

Основные источники tracing:

```text
goroutine stacks
globals
runtime-managed roots
```

```text
goroutine stack
      │
      ▼
 Request
      │
      ▼
 Session
      │
      ▼
 []Token
```

## 42. Goroutine leak → memory retention

Leaked goroutine может удерживать references на stack.

```text
leaked goroutine
↓
stack
↓
heap pointer
↓
large object graph
```

Concurrency bug превращается в memory problem.

---

# Блок 10. Архитектура Go GC

## 43. Основные свойства

Go 1.27 collector на высоком уровне:

- tracing;
- precise;
- concurrent;
- parallel;
- mark-and-sweep;
- non-generational;
- non-compacting;
- использует write barrier.

## 44. Tracing

```text
roots
↓
object
↓
object
↓
object
```

Это не reference counting.

## 45. Precise

```go
type User struct {
    ID      uint64
    Session *Session
    Active  bool
}
```

GC знает:

```text
ID      → не pointer
Session → pointer
Active  → не pointer
```

## 46. Parallel и concurrent

Parallel:

```text
CPU0 → GC
CPU1 → GC
CPU2 → GC
```

Concurrent:

```text
CPU0 → application
CPU1 → application
CPU2 → GC
CPU3 → GC
```

## 47. Non-generational

Нет базовой архитектуры:

```text
young generation
old generation
```

## 48. Non-compacting

После GC heap может выглядеть:

```text
[A][free][C][free][E][free][G]
```

а не обязательно быть физически уплотнён.

---

# Блок 11. Зачем write barrier

## 49. Concurrent graph mutation

Было:

```text
A → B
```

GC уже просмотрел A.

Application делает:

```text
A → C
```

Heap graph изменился прямо во время tracing.

Без дополнительного механизма collector может потерять reachable object.

## 50. STW было бы проще

```text
application stopped
↓
heap graph frozen
↓
GC спокойно обходит graph
```

Но это даёт большие pauses.

Concurrent GC выбирает более сложный путь: application продолжает работать, а runtime поддерживает correctness специальными barriers.

---

# Блок 12. Write barrier

## 51. Что это

Упрощённо:

```text
обычный store:
slot = newPointer
```

во время mark phase становится концептуально:

```text
GC bookkeeping
+
slot = newPointer
```

Компилятор/runtime оптимизируют этот path, но смысл такой: pointer mutation должна быть видима collector.

## 52. Old и new pointers

```text
before:
slot → old

after:
slot → new
```

Hybrid write barrier сохраняет tracing invariant так, чтобы collector не потерял важные references при concurrent mutation.

## 53. Почему barrier стоит CPU

```text
check
bookkeeping
buffer/shade
store
```

GC cost распределяется не только по background workers. Часть цены платит mutator.

## 54. Barrier работает по фазам

Упрощённо:

```text
GC off
↓
barrier mostly off

concurrent mark
↓
barrier on
```

---

# Блок 13. Green Tea GC

## 55. Старая проблема: pointer chasing

Классическая учебная картина:

```text
найди A
↓
scan A
↓
найди B
↓
scan B
```

Но физически:

```text
A → page 100
B → page 8000
C → page 42
D → page 900
```

CPU часто получает cache misses.

## 56. Почему locality важна

```text
L1 hit → очень быстро
L1 miss → L2
L2 miss → LLC
LLC miss → DRAM
```

Heap tracing может упираться в memory subsystem CPU, а не в арифметику.

## 57. Идея Green Tea

Вместо беспорядочного object-at-a-time scan runtime старается группировать работу по spans/pages.

```text
span 1:
    A
    C
    E

span 40:
    B

span 900:
    D
```

Цель:

```text
лучше reuse cache lines
меньше повторной metadata work
лучше locality
```

## 58. Что для разработчика не изменилось

```text
heap object
↓
unreachable
↓
runtime eventually reclaims it
```

Green Tea — implementation detail current runtime.

## 59. Mark и scan — не одно и то же

```text
marked = object обнаружен reachable
scanned = outgoing pointers обработаны
```

Возможное состояние:

```text
marked = yes
scanned = no
```

## 60. Снова per-P locality

Scheduler:

```text
P → local run queue → steal/global
```

Allocator:

```text
P → mcache → mcentral
```

GC:

```text
P → local work → share/steal
```

Общий runtime pattern:

> Горячую работу держим локально, глобальную координацию делаем реже.

## Что важно запомнить

1. Concurrent GC обязан учитывать изменения heap graph.
2. Write barrier сохраняет correctness tracing.
3. Barrier добавляет runtime cost к части pointer writes.
4. Green Tea оптимизирует locality marking/scanning work.
5. Green Tea — detail current runtime, не гарантия спецификации.

---

# Блок 14. GC cycle как timeline

## 61. Полный цикл

```text
application running
        │
        ▼
GC trigger
        │
        ▼
short STW preparation
        │
        ├── switch phase
        ├── enable write barrier
        └── prepare roots/work
        │
        ▼
world resumed
        │
        ▼
concurrent mark
        │
        ├── background workers
        ├── stack scans
        ├── write barriers
        └── mutator assists
        │
        ▼
mark termination
        │
        ▼
short STW
        │
        ▼
sweep
        │
        ├── background
        └── allocation-driven
```

```text
GC cycle
≠
одна длинная pause
```

## 62. Зачем остаётся STW

Есть моменты, когда runtime выгоднее кратко получить глобально согласованное состояние.

Низкие STW pauses — лишь одна часть стоимости GC.

---

# Блок 15. Mark workers

## 63. Кто выполняет GC

```text
dedicated workers
fractional workers
idle workers
mutator assists
```

Нет одного волшебного «GC thread».

## 64. Dedicated workers

```text
P0 → request
P1 → request
P2 → GC worker
P3 → request
```

GC получает гарантированный CPU budget.

## 65. Background utilization

Current pacer target для background marking ориентируется примерно на 25% `GOMAXPROCS`.

```text
GOMAXPROCS=8
≈ 2 CPU worth background mark target
```

Это control target, не строгая гарантия.

## 66. Fractional workers

Если нельзя выделить целое количество P для нужной доли:

```text
worker работает часть времени
↓
остальное время P выполняет mutator work
```

## 67. Idle workers

Если P иначе простаивал бы, runtime может использовать его для GC.

Поэтому высокий процент `gcBgMarkWorker` в CPU profile не всегда означает, что GC отнял всю эту CPU capacity у requests.

---

# Блок 16. GC pacer

## 68. Почему нельзя стартовать на heap goal

```text
heap goal = 2 GB
```

Если GC начался только на 2 GB, application продолжит allocation во время marking:

```text
2.0
2.1
2.2
2.3 GB
```

Мы уже опоздали.

## 69. Goal и trigger

```text
trigger = стартовая линия

goal = желательная финишная граница heap
```

GC должен стартовать раньше goal.

## 70. От чего зависит trigger

```text
allocation rate
scan throughput
root work
CPU availability
heap goal
memory limit
```

Это control problem.

## 71. Почему pacer

Не таймер:

```text
GC раз в N секунд
```

А feedback loop:

```text
наблюдаем workload
↓
оцениваем будущую collector work
↓
подбираем trigger и assist pressure
```

---

# Блок 17. GOGC глубже

## 72. Смысл

Упрощённо:

```text
heap goal
≈
live memory
+
growth budget controlled by GOGC
```

Roots/scannable work тоже участвуют в современной pacing model.

## 73. GOGC=100

Если после GC:

```text
live ≈ 1 GB
```

runtime получает примерно ещё один сопоставимый growth budget до target с поправками на roots и memory limit.

## 74. GOGC ниже

```text
GOGC ↓
↓
heap goal ↓
↓
GC frequency ↑
↓
GC CPU ↑
↓
memory ↓
```

## 75. GOGC выше

```text
GOGC ↑
↓
heap goal ↑
↓
GC frequency ↓
↓
GC CPU ↓
↓
memory ↑
```

Пока не вмешается `GOMEMLIMIT`.

---

# Блок 18. Mutator assists

## 76. Что такое mutator

Mutator — application, изменяющая heap:

```text
allocates objects
writes pointers
changes graph
```

## 77. Application быстрее collector

```text
collector scans 1 GB/s
application allocates 8 GB/s
```

Если application не ограничивать:

```text
heap goal exceeded
↓
memory blow-up
```

## 78. GC debt

Концептуально:

```text
ты аллоцировал N bytes
↓
создал дополнительную GC work
↓
если collector отстаёт — помоги
```

```text
request goroutine
↓
malloc
↓
assist debt
↓
GC marking
↓
return to handler
```

## 79. Почему это влияет на p99

```text
RPS ↑
↓
allocation rate ↑
↓
collector отстаёт
↓
assists ↑
↓
request goroutine выполняет GC work
↓
handler wall time ↑
↓
p95/p99 ↑
```

Никакой огромной STW pause не требуется.

## 80. `gcAssistAlloc` в CPU profile

Если cumulative profile показывает заметный:

```text
runtime.gcAssistAlloc
```

это сильный сигнал allocation pressure или tight memory budget.

---

# Блок 19. Sweep

## 81. Mark ещё не освобождает slot

```text
A live
B dead
C live
D dead
```

Mark ответил:

```text
кто reachable?
```

Sweep отвечает:

```text
что allocator снова может использовать?
```

## 82. Bitmap переход

Учебно:

```text
allocBits:   1 1 1 1
gcmarkBits:  1 0 1 0
```

После sweep:

```text
A occupied
B free
C occupied
D free
```

## 83. Sweep может быть concurrent

```text
background sweep
```

и:

```text
allocation-driven sweep
```

Allocator может sweep'ить span, когда хочет его использовать.

## 84. sweepgen

Runtime использует generation state, чтобы понимать lifecycle span без полного глобального обхода только ради проверки «уже swept или ещё нет?».

---

# Блок 20. Sweep ≠ scavenging

## 85. GC освободил объект — RAM ещё не обязана вернуться Linux

После sweep:

```text
dead object
↓
free slot
```

Memory свободна **для Go allocator**.

## 86. Уровни reclaim

```text
object unreachable
↓
not marked
↓
sweep
↓
slot/page reusable by Go
↓
scavenger
↓
OS may reclaim physical pages
```

## 87. Почему runtime не отдаёт всё сразу

Если traffic скоро вернётся, повторное использование уже mapped pages дешевле, чем постоянно отдавать и снова получать их от kernel.

Trade-off:

```text
reuse speed
↔
low RSS
```

---

# Блок 21. Scavenger

## 88. Задача

```text
GC → dead objects
sweep → free Go pages
scavenger → release physical backing to OS
```

## 89. Linux

Runtime использует platform-specific VM primitives, включая `madvise` modes вроде `MADV_FREE` и `MADV_DONTNEED` в зависимости от условий.

Учебный вывод:

> Runtime управляет virtual memory через механизмы ОС; «free memory» — не одна универсальная операция.

## 90. Virtual ≠ physical

Большой virtual address reservation не означает такой же объём resident RAM.

Поэтому VSS часто мало полезен сам по себе.

Для production важнее:

```text
RSS
heap classes
released memory
cgroup memory
```

---

# Блок 22. HeapAlloc и RSS

## 91. Пример

До GC:

```text
HeapAlloc = 4 GB
RSS       = 4.5 GB
```

После:

```text
HeapAlloc = 1 GB
RSS       = 4.0 GB
```

Это ещё не доказательство leak.

## 92. Четыре разные величины

```text
live/object memory
free heap memory
released heap memory
RSS
```

Они отвечают на разные вопросы.

## 93. Почему RSS не обязан падать мгновенно

Даже если runtime сообщил kernel, что pages можно reclaim, фактический reclaim может зависеть от kernel policy и memory pressure.

---

# Блок 23. GOMEMLIMIT глубже

## 94. Почему GOGC недостаточно

GOGC задаёт относительный trade-off.

Container даёт абсолютный budget:

```text
memory limit = 1 GiB
```

Runtime нужен отдельный сигнал о допустимом memory footprint — `GOMEMLIMIT`.

## 95. GOMEMLIMIT ≠ max heap

Он относится к memory, которой управляет Go runtime, и учитывает больше, чем live heap.

Удобная модель:

```text
controlled Go memory
≈
/memory/classes/total:bytes
-
/memory/classes/heap/released:bytes
```

## 96. Почему limit soft

```text
GOMEMLIMIT = 500 MiB
live ≈ 480 MiB
```

Если требовать абсолютного соблюдения:

```text
GC
↓
чуть allocation
↓
GC
↓
чуть allocation
↓
GC
...
```

можно почти полностью сжечь CPU.

## 97. GC CPU limiter

Runtime ограничивает степень, до которой GC может душить application ради memory limit.

Иногда временно превысить soft limit лучше, чем потерять progress из-за бесконечного GC.

---

# Блок 24. Kubernetes и cgroup

## 98. Два лимита

```text
GOMEMLIMIT
→ soft runtime budget

cgroup memory limit
→ kernel-enforced hard boundary
```

## 99. Почему нужен headroom

Плохо:

```text
container limit = 1 GiB
GOMEMLIMIT      = 1 GiB
```

У процесса есть memory outside the simple managed-heap picture:

```text
cgo/native allocations
some mmap
thread/platform overhead
other external memory
```

Практический подход:

```text
hard container limit
↓
safety margin
↓
GOMEMLIMIT
↓
load test
```

---

# Блок 25. Memory pressure → CPU pressure

## 100. Главная production-цепочка

```text
memory budget ↓
↓
heap goal ↓
↓
GC frequency ↑
↓
background mark CPU ↑
↓
assist CPU ↑
↓
CPU for requests ↓
↓
latency ↑
↓
throughput ↓
```

## 101. Типичный инцидент

Было:

```text
pod limit = 2 GiB
GOMEMLIMIT = 1.8 GiB
live set ≈ 750 MiB
```

Стало:

```text
pod limit = 1 GiB
GOMEMLIMIT = 900 MiB
```

Теперь у collector гораздо меньше runway между live set и goal.

Внешне:

```text
RAM уменьшили
CPU вырос
p99 вырос
```

На самом деле это одна runtime-цепочка.

---

# Блок 26. Allocation pressure и live heap

## 102. Сервис A: churn

```text
live heap = 200 MB
allocation rate = 8 GB/s
```

Heap не растёт, но collector постоянно перерабатывает новый мусор.

Симптом:

```text
RSS стабилен
GC CPU высокий
cycles frequent
```

## 103. Сервис B: retention

```text
allocation rate = 200 MB/s
live heap:
500 MB
700 MB
1 GB
1.5 GB
```

Здесь objects остаются reachable.

Причины:

```text
unbounded cache
queue backlog
goroutine leak
slice/map retention
```

## 104. Heap profile modes

```text
alloc_space → allocation churn / hotspots
inuse_space → retained live memory
```

---

# Блок 27. Как это видно в pprof

## 105. `runtime.mallocgc`

Большое cumulative время может означать большой allocation volume.

Но внутри call tree могут быть assists, поэтому одного symbol мало.

## 106. `runtime.gcAssistAlloc`

Сильный сигнал:

```text
allocating goroutines помогают GC
```

Проверяем allocation rate и memory budget.

## 107. `runtime.gcBgMarkWorker`

Смотрим:

```text
GC frequency
scannable heap
object graph
idle vs dedicated workers
```

Не делаем вывод «GC сломан» только по имени функции.

---

# Блок 28. runtime/metrics

## 108. Heap state

```text
/gc/heap/live:bytes
/gc/heap/goal:bytes
/gc/heap/allocs:bytes
/gc/heap/frees:bytes
```

## 109. Scan work

```text
/gc/scan/heap:bytes
/gc/scan/stack:bytes
```

## 110. GC CPU

```text
/cpu/classes/gc/mark/assist:cpu-seconds
/cpu/classes/gc/mark/dedicated:cpu-seconds
/cpu/classes/gc/mark/idle:cpu-seconds
/cpu/classes/gc/total:cpu-seconds
```

## 111. Memory classes

```text
/memory/classes/heap/objects:bytes
/memory/classes/heap/free:bytes
/memory/classes/heap/released:bytes
/memory/classes/heap/unused:bytes
```

## 112. Allocator metadata

```text
/memory/classes/metadata/mcache/inuse:bytes
/memory/classes/metadata/mspan/inuse:bytes
```

Memory manager сам тоже занимает memory.

---

# Блок 29. Один объект: полный lifecycle

## 113. Исходный код

```go
func loadUser() *User {
    u := &User{ID: 42, Name: "Alice"}
    return u
}
```

## 114. Compiler

```text
pointer переживает frame
↓
heap allocation
```

## 115. Allocator class

Пусть учебно:

```text
size = 32 B
contains pointers = yes
↓
32-byte scan spanClass
```

## 116. P-local allocation

```text
G
↓
P2
↓
mcache2
↓
32-byte scan mspan
↓
free slot
↓
object
```

## 117. Span заполнен

```text
mcache2
↓
mcentral
↓
partially-free span
```

## 118. Central не хватает span

```text
mcentral
↓
mheap
↓
page allocator
↓
возможно OS
```

## 119. Heap растёт

Pacer следит за:

```text
heap live
heap goal
allocation rate
scan work
```

Достигается trigger → начинается GC.

## 120. Object live

```text
root/cache
↓
User
```

GC marks User и сканирует pointer fields.

## 121. Object dead

Reference исчезла.

Следующий GC не находит User.

Sweep:

```text
slot reusable
```

## 122. Но RSS может остаться

```text
slot free for Go
↓
span/page may remain mapped
↓
scavenger later releases pages
↓
OS may reclaim RAM
```

---

# Блок 30. Что меняется под нагрузкой

## 123. Один request

```text
20 allocations
10 KB
```

Почти незаметно.

## 124. 50 000 RPS

```text
20 alloc/request × 50 000
=
1 000 000 allocations/sec
```

```text
10 KB/request × 50 000
≈
500 MB/s allocation rate
```

## 125. Добавили временные objects

```text
string ↔ []byte
fmt.Sprintf
temporary maps
JSON copies
reflection
```

Allocation rate может вырасти в разы при почти прежнем live heap.

## 126. Tail latency

Assists не обязаны распределяться идеально равномерно.

```text
average +5%
p99 +50%
```

GC pressure часто сначала проявляется в tail latency.

---

# Блок 31. Что оптимизировать

## 127. Не начинать с GOGC

Сначала спросить:

```text
почему приложение столько аллоцирует?
```

Tuning GC перераспределяет цену, но не уничтожает ненужный мусор.

## 128. Идём до allocation site

```text
symptom
↓
CPU pprof
↓
alloc profile
↓
benchmark -benchmem
↓
escape diagnostics
↓
конкретная строка кода
```

## 129. Один removed allocation даёт несколько выигрышей

```text
allocator work ↓
GC work ↓
cache pressure ↓
```

---

# Блок 32. `sync.Pool`

## 130. Идея

```text
allocate → use → garbage
```

заменяем на:

```text
get → use → reset → put
```

## 131. Цена

```text
allocation rate ↓
```

но возможно:

```text
retained memory ↑
complexity ↑
stale state risk ↑
```

## 132. Correctness

Нужно:

```text
reset object
не использовать after Put
не протащить request-specific data
```

## 133. Когда использовать

```text
profile → hotspot → reuse possible → benchmark → pool
```

а не:

```text
allocation exists → pool everything
```

---

# Блок 33. Практическая диагностика

## 134. CPU высокий, memory нормальная

```text
CPU profile
↓
mallocgc / gcAssistAlloc / gcBgMarkWorker?
↓
alloc profile
```

## 135. Memory растёт

Снимать `inuse_space` во времени:

```text
t0
t1
t2
```

Искать call sites, удерживающие всё больше live memory.

## 136. Goroutines растут

Смотреть goroutine profile.

В Go 1.27 есть отдельный `goroutineleak` profile для класса permanently blocked goroutines, которые runtime способен определить через reachability. Он не может обнаружить все виды leaks.

## 137. HeapAlloc упал, RSS нет

Смотреть вместе:

```text
heap objects
heap free
heap released
RSS
```

## 138. После уменьшения RAM вырос CPU

Проверять:

```text
GOMEMLIMIT
heap goal
GC cycles
assist CPU
GC total CPU
```

---

# Блок 34. Практика к лекции

## 139. Эксперимент: size classes

```go
type S16 struct { A, B uint64 }
type S24 struct { A, B, C uint64 }
type S40 struct { A, B, C, D, E uint64 }
```

Forced escape benchmark:

```go
var sink any

func BenchmarkS16(b *testing.B) {
    for b.Loop() {
        sink = new(S16)
    }
}
```

```bash
go test -bench=. -benchmem
```

Смотреть:

```text
object size
B/op
allocs/op
size-class effects
```

## 140. Эксперимент: scan vs noscan

```go
type Raw struct {
    A, B, C, D uint64
}

type Refs struct {
    A, B, C, D *int
}
```

Создать большие live heaps.

Сравнить:

```text
heap bytes
/gc/scan/heap:bytes
GC CPU
```

## 141. Эксперимент: allocation churn

```go
func handler(w http.ResponseWriter, r *http.Request) {
    chunks := make([][]byte, 0, 1000)
    for range 1000 {
        chunks = append(chunks, make([]byte, 1024))
    }
    fmt.Fprintln(w, len(chunks))
}
```

Нагрузить и измерить:

```text
alloc rate
GC cycles
GC CPU
assist CPU
latency
```

Потом уменьшить allocations и повторить.

## 142. Эксперимент: GOMEMLIMIT

```bash
GOMEMLIMIT=2GiB ./app
GOMEMLIMIT=1GiB ./app
GOMEMLIMIT=600MiB ./app
```

Сравнить:

```text
heap goal
GC cycles/sec
GC CPU
assist CPU
p95/p99
RSS
```

## 143. Эксперимент: scavenger

```text
allocate several GB
↓
hold
↓
drop references
↓
GC
↓
observe
```

Смотреть:

```text
heap objects
heap free
heap released
RSS
```

---

# Блок 35. Версионные границы

## 144. Устойчивая модель

Полезно преподавать как долговечные принципы:

```text
stack vs heap
escape analysis
size-segregated allocator
per-P allocation fast path
tracing GC
concurrent marking
write barriers
GC pacing
mutator assists
sweep
scavenging
GOGC
GOMEMLIMIT
```

## 145. Current implementation details

Явно помечать как версионно-зависимые:

```text
конкретные поля mspan
внутренние mallocgc paths
Green Tea queues
точные specialization thresholds
madvise policy
pacer constants
```

Исходники Go 1.27 — не спецификация языка.

---

# Блок 36. Полная причинно-следственная модель

## 146. От allocation до latency

```text
source code
↓
value escapes
↓
heap allocation
↓
size/span class
↓
mcache
↓
mspan slot
↓
heap growth
↓
pacer sees pressure
↓
GC cycle
↓
background mark
↓
write barriers
↓
mutator assist
↓
request goroutine spends CPU in GC
↓
handler finishes later
↓
p99 ↑
```

## 147. От dead object до RSS

```text
object unreachable
↓
not marked
↓
sweep
↓
slot reusable
↓
possibly whole span/pages free
↓
scavenger
↓
OS can reclaim physical pages
↓
RSS may decrease
```

## 148. От memory limit до CPU

```text
container memory reduced
↓
GOMEMLIMIT reduced
↓
heap goal reduced
↓
GC starts more frequently
↓
mark CPU ↑
↓
assist CPU ↑
↓
less CPU for handlers
↓
latency ↑
```

---

# Что важно запомнить

1. **Small heap allocation в common case обслуживается локально через per-P allocator state.** Дорогие глобальные уровни нужны в основном для refill и роста heap.
2. **`mspan` — ключевая единица, связывающая allocator и GC.** Span описывает run runtime pages и для small objects разбит на slots одного span class.
3. **Размер объекта — только половина истории.** Наличие pointers определяет scan/noscan и напрямую влияет на работу collector.
4. **GC cost определяется не только live heap.** Важны allocation rate, scannable heap, roots, shape pointer graph и memory budget.
5. **Concurrent GC не бесплатный.** Цена распределена между background workers, write barriers и mutator assists.
6. **Green Tea с Go 1.26 — текущая реализация default collector.** Его ключевая идея — улучшить locality marking/scanning work.
7. **Heap goal и GC trigger — разные понятия.** Pacer должен начать collector заранее.
8. **Mutator assist напрямую связывает allocations с request latency.** Allocating goroutine может сама выполнять GC work.
9. **Sweep и scavenger решают разные задачи.** Sweep возвращает memory allocator'у Go; scavenger помогает вернуть physical backing ОС.
10. **HeapAlloc, free heap, released heap и RSS нельзя смешивать.** Они описывают разные слои memory management.
11. **`GOMEMLIMIT` — soft runtime budget, cgroup limit — внешний hard constraint.** Между ними нужен запас.
12. **Главная optimization strategy — уменьшать ненужные allocations и pointer-rich live data после измерений, а не механически крутить GC knobs.**

---

# Итоговая схема

```text
                    SOURCE CODE
                        │
                        ▼
                 escape analysis
                        │
                        ▼
                 heap allocation
                        │
                        ▼
              size + pointer layout
                        │
                        ▼
                    spanClass
                        │
                        ▼
                     mcache
                        │
                        ▼
                     mspan
                  free slot?
                  │       │
                yes       no
                  │       ▼
                  │    mcentral
                  │       │
                  │       ▼
                  │     mheap
                  │       │
                  │       ▼
                  │  page allocator
                  │       │
                  │       ▼
                  │      OS
                  │
                  ▼
               object live
                  │
                  ▼
              heap growth
                  │
                  ▼
                pacer
                  │
                  ▼
              GC trigger
                  │
                  ▼
        concurrent Green Tea mark
          │        │         │
          │        │         │
       workers   barrier   assists
          │        │         │
          └────────┴─────────┘
                  │
                  ▼
                 sweep
                  │
          ┌───────┴────────┐
          ▼                ▼
     reuse by Go       free pages
                           │
                           ▼
                       scavenger
                           │
                           ▼
                           OS
```

Финальный инженерный вывод:

> В Go heap allocation — не просто операция «взяли память». Это вход в целую runtime-систему. Fast path может быть очень дешёвым, но объект способен позже породить mark work, write-barrier work, assists, sweep work, RSS pressure и kernel memory-management work. Поэтому настоящая цена allocation проявляется не только в момент её выполнения, а на всём жизненном цикле объекта.
