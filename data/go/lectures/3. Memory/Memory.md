# 3. Go memory, GC и pprof

> Версионная привязка: материал ориентирован на стандартный Go toolchain версии 1.27.x.
>
> В этой лекции важно постоянно разделять:
>
> - **гарантии языка** — что обещает спецификация;
> - **решения compiler/runtime** — stack/heap placement, GC и конкретные оптимизации;
> - **production-поведение** — allocations, GC CPU, RSS, latency, goroutine leaks.
>
> Детали `mcache`, `mspan`, `mcentral`, Green Tea queues, sweep generations и scavenger internals разбираются в отдельной дополнительной лекции.

Главная цепочка лекции:

```text
значение в коде
      ↓
lifetime
      ↓
escape analysis
      ↓
stack или heap
      ↓
allocations
      ↓
heap growth
      ↓
GC
      ↓
CPU / memory pressure
      ↓
pprof
      ↓
конкретная строка кода
```

Цель лекции — чтобы студент после неё не говорил:

> «GC съел память»  
> «указатель значит heap»  
> «маленькая GC pause значит GC дешёвый»  
> «RSS растёт — значит leak»

Вместо этого он должен уметь построить причинно-следственную цепочку.

---

# Блок 1. С чего вообще начинается разговор о памяти

## 1. Backend живёт не в абстрактной памяти

Возьмём HTTP-сервис:

```go
func handler(w http.ResponseWriter, r *http.Request) {
    user := User{
        ID:   42,
        Name: "Alice",
    }

    data, _ := json.Marshal(user)
    w.Write(data)
}
```

На уровне бизнес-кода всё просто.

Но процессу физически нужны ресурсы:

```text
код программы
goroutine stacks
heap objects
runtime metadata
OS thread stacks
network buffers
memory mappings
```

И в какой-то момент production говорит:

```text
CPU = 95%
RSS = 1.8 GB
p99 = 900 ms
GC CPU высокий
goroutines = 40 000
```

Чтобы понять, почему это произошло, одной синтаксической модели Go недостаточно.

Нужно понимать **lifetime данных**.

---

## 2. Главный вопрос — не «где объявлена переменная»

Плохое упрощение:

```text
локальная переменная → stack
new() → heap
указатель → heap
```

Эта таблица ломается почти сразу.

Полезнее задавать вопрос:

> Как долго значение должно существовать и кто может на него ссылаться?

Например:

```go
func answer() int {
    x := 42
    return x
}
```

`x` нужен только во время вызова `answer`.

А теперь:

```go
func answer() *int {
    x := 42
    return &x
}
```

После `return` вызывающая функция всё ещё получает адрес `x`.

Значит lifetime значения переживает execution frame функции.

Именно здесь возникает задача compiler:

```text
где разместить значение,
чтобы его lifetime был корректным?
```

---

# Блок 2. Stack

## 3. Stack — сначала ментальная модель

При вызове функции ей нужна память для выполнения.

Учебно можно представить frame:

```text
foo()
┌─────────────────┐
│ arguments       │
│ local values    │
│ temporary data  │
│ return state    │
└─────────────────┘
```

Функция вызывает другую:

```text
main
 ↓
handle
 ↓
parse
 ↓
validate
```

Получаем:

```text
goroutine stack

┌────────────┐
│ validate   │
├────────────┤
│ parse      │
├────────────┤
│ handle     │
├────────────┤
│ main       │
└────────────┘
```

Когда `validate` заканчивается, её frame больше не нужен.

---

## 4. Почему stack allocation дешёвая

В очень упрощённой модели stack allocation похожа на:

```text
stack pointer
      ↓
сдвинули границу
      ↓
готово
```

Когда функция возвращается:

```text
frame больше не нужен
↓
stack pointer возвращается
```

Не нужно:

```text
искать отдельный свободный object
запоминать его lifetime в heap
потом искать его GC
```

Поэтому stack allocation обычно значительно дешевле heap allocation.

---

## 5. Кто освобождает stack value

```go
func calculate() int {
    x := 10
    y := 20
    return x + y
}
```

Когда frame `calculate` исчезает:

```text
x
y
```

исчезают вместе с ним.

Никакой GC не обязан отдельно искать:

```text
x больше не нужен?
y больше не нужен?
```

Lifetime уже структурирован call stack.

---

## 6. У каждой goroutine свой stack

Это важная связь с конкурентностью.

```text
G1 → stack1
G2 → stack2
G3 → stack3
...
```

Если одновременно существует 100 000 goroutines, у каждой есть собственное stack state.

Но было бы слишком дорого заранее выдавать каждой goroutine огромный stack.

Поэтому goroutine stacks стартуют небольшими и runtime может увеличивать их по мере необходимости.

---

## 7. Stack может расти

Представим глубокий call chain:

```text
f1
↓
f2
↓
f3
↓
...
↓
f1000
```

Или recursive algorithm.

Текущего stack space может не хватить.

Runtime Go умеет увеличить stack goroutine.

Учебная модель:

```text
старый stack слишком мал
↓
runtime получает больший stack
↓
переносит stack data
↓
обновляет известные references
↓
goroutine продолжает работу
```

Это implementation detail runtime, но полезное следствие:

> Размер goroutine stack не является фиксированной величиной на весь lifetime goroutine.

---

## 8. Stack тоже потребляет память

Иногда говорят:

> «Goroutine почти бесплатная».

Нет.

Даже если stack стартует маленьким, goroutine:

```text
имеет stack
имеет runtime metadata
может удерживать heap references
может вырасти
```

Поэтому leak из сотен тысяч goroutines — memory problem даже до учёта heap objects, которые они удерживают.

---

## Что важно запомнить

1. Stack связан с execution frames функций.
2. Stack values удобно освобождать вместе с завершением frame.
3. У каждой goroutine свой stack.
4. Goroutine stack способен расти.
5. Stack memory тоже не бесплатна.

---

# Блок 3. Heap

## 9. Когда structured lifetime stack уже недостаточен

```go
func createUser() *User {
    u := User{
        ID: 42,
    }

    return &u
}
```

После возврата:

```text
createUser frame
```

закончился.

Но объект должен остаться доступным вызывающему коду.

Значит storage объекта не может зависеть от lifetime этого frame.

Здесь появляется heap.

---

## 10. Что такое heap в полезной модели

Heap — managed memory для значений, lifetime которых нельзя удобно связать только с текущим stack frame.

Очень грубо:

```text
stack:
lifetime следует call structure

heap:
lifetime определяется reachability
```

Это важнее определения:

> «Heap — большая область динамической памяти».

Нас интересует именно различие lifetime.

---

## 11. Кто освобождает heap object

Stack frame уничтожается структурно.

С heap всё сложнее.

Например:

```text
A → B → C
```

Пока `A` reachable, `B` и `C` тоже могут быть нужны.

Потом reference на `A` исчезает.

Теперь runtime должен установить:

```text
существует ли ещё какой-либо путь к A/B/C?
```

Именно эту задачу решает GC.

---

## 12. Stack и heap — не синтаксические конструкции языка

Go specification не обещает:

```text
new(T) обязан heap
локальная переменная обязана stack
```

Storage managed implementation.

Compiler может изменить placement между версиями, не меняя корректность программы.

Правильный уровень рассуждения:

```text
семантика программы
↓
compiler определяет storage strategy
```

---

# Блок 4. Разрушаем мифы про stack и heap

## 13. `new(T)` не означает «heap»

```go
func f() int {
    p := new(int)
    *p = 42
    return *p
}
```

На уровне языка:

```text
new(int)
```

создаёт variable и возвращает pointer.

Но compiler может доказать:

```text
pointer не переживает вызов f
```

и оставить storage вне heap либо оптимизировать его ещё сильнее.

Следовательно:

```text
new
≠
гарантированный heap allocation
```

---

## 14. `&x` не означает «heap»

```go
func f() {
    x := 42
    p := &x
    consume(p)
}
```

Сам факт взятия адреса не требует heap.

Вопрос:

```text
куда pointer уходит?
как долго он живёт?
может ли compiler доказать lifetime?
```

---

## 15. Локальная переменная может попасть в heap

```go
func makeCounter() func() int {
    x := 0

    return func() int {
        x++
        return x
    }
}
```

Closure переживает `makeCounter`.

Значит captured state `x` должен жить дольше frame функции.

Compiler должен обеспечить этот lifetime.

---

## 16. Большое значение тоже может изменить решение compiler

Даже если значение логически могло бы жить на stack, compiler/runtime могут учитывать ограничения реализации.

Большие local objects, inlining и конкретные optimization passes могут влиять на storage decision.

Поэтому нельзя строить лекцию вокруг списка:

```text
конструкция X всегда stack
конструкция Y всегда heap
```

Надёжнее:

```text
изучаем lifetime
↓
проверяем compiler diagnostics
↓
измеряем allocations
```

---

# Блок 5. Escape analysis

## 17. Зачем compiler вообще анализирует escape

```go
func f() *int {
    x := 42
    return &x
}
```

Compiler видит flow:

```text
x
↓
&x
↓
return
↓
caller
```

Reference выходит за lifetime frame.

Говорят:

```text
x escapes
```

То есть compiler не может оставить storage только внутри обычного lifetime текущего frame.

---

## 18. Escape — это анализ потока references

Не сводим к:

```text
есть pointer → escape
```

Compiler анализирует:

```text
value
↓
reference
↓
куда сохраняется?
↓
кто может пережить текущую функцию?
```

Типичные причины:

```text
return pointer
closure capture
store into longer-lived object
store in heap structure
unknown/indirect call behavior
large object/compiler constraints
```

---

## 19. Пример: return pointer

```go
func makeID() *int {
    id := 42
    return &id
}
```

Lifetime:

```text
frame makeID закончился
↓
caller всё ещё использует pointer
```

Storage обязан пережить frame.

Это очевидный escape case.

---

## 20. Пример: pointer остаётся локальным

```go
func local() int {
    x := 42
    p := &x
    return *p
}
```

Pointer существует.

Но:

```text
p не уходит наружу
```

Compiler может оставить value вне heap.

Снова:

```text
pointer
≠
heap
```

---

## 21. Пример: closure

```go
func counter() func() int {
    n := 0

    return func() int {
        n++
        return n
    }
}
```

Closure хранит доступ к `n`.

После:

```text
counter() returned
```

`n` всё ещё нужен.

Это lifetime extension.

---

## 22. Пример: сохранение в более долгоживущий object

```go
type Holder struct {
    P *int
}

func put(h *Holder) {
    x := 42
    h.P = &x
}
```

После `put`:

```text
h.P
```

может использовать `x`.

Значит `x` не может исчезнуть вместе с frame.

---

## 23. Inlining может изменить escape result

До inlining compiler может видеть функцию отдельно.

После inlining body вызываемой функции становится виден caller context.

Иногда теперь можно доказать:

```text
reference не переживает caller
```

и убрать heap allocation.

Следствие:

> Allocation count может измениться после seemingly unrelated compiler upgrade.

---

## 24. Go продолжает улучшать stack allocation

Go 1.26 расширил возможности compiler размещать некоторые constant-sized slices на stack.

Это хороший пример:

```text
код не изменился
↓
семантика не изменилась
↓
compiler доказал больше
↓
heap allocations стало меньше
```

Поэтому stack/heap placement — implementation detail, а не API contract.

---

# Блок 6. Как смотреть escape analysis

## 25. Compiler diagnostics

Практический инструмент:

```bash
go build -gcflags="-m=2" ./...
```

Для отдельного файла:

```bash
go build -gcflags="-m=2" main.go
```

Можно увидеть сообщения вроде:

```text
x escapes to heap
moved to heap: x
does not escape
```

---

## 26. Не читать `-m` как готовый performance report

Escape report отвечает:

```text
почему compiler принял storage decision
```

Он не отвечает:

```text
это bottleneck?
```

Пример:

```text
1 heap allocation
```

в функции, вызываемой раз в час, неинтересна.

Та же allocation:

```text
× 100 000 RPS
```

уже может быть существенна.

---

## 27. Правильная последовательность

Не:

```text
запустить -gcflags=-m
↓
оптимизировать всё красное
```

А:

```text
наблюдаем CPU/memory problem
↓
profile / benchmark
↓
находим allocation hotspot
↓
escape analysis объясняет причину
```

Compiler diagnostics — инструмент объяснения, а не автоматический backlog.

---

## Что важно запомнить

1. Escape analysis исследует lifetime/reference flow.
2. Pointer сам по себе не означает escape.
3. Closure, return и long-lived references часто приводят к escape.
4. Результаты могут меняться после compiler improvements.
5. `-gcflags="-m=2"` помогает объяснять allocations, но не заменяет profiling.


---

# Блок 7. Allocation

## 28. Что именно называется allocation

В контексте performance обычно интересует heap allocation:

```text
runtime должен получить storage для нового heap object
```

Но полезно различать три вещи:

```text
allocation count
allocated bytes
live bytes
```

Это разные характеристики.

---

## 29. Почему allocation rate важнее одной allocation

Одна allocation:

```text
256 B
```

ничего не значит.

Теперь:

```text
256 B
×
2 000 000 allocations/sec
≈
512 MB/sec
```

Runtime каждую секунду пропускает через allocator огромный поток objects.

Большинство может умереть почти сразу.

Memory usage может оставаться стабильной.

Но GC work будет большой.

---

## 30. Live heap и allocation rate — разные оси

Сервис A:

```text
live heap = 200 MB
allocation rate = 5 GB/s
```

Сервис B:

```text
live heap = 3 GB
allocation rate = 100 MB/s
```

У A:

```text
много churn
```

У B:

```text
много retained/live memory
```

Оба могут иметь memory/GC проблемы, но причины будут разными.

---

## 31. Lifetime distribution

Представим три типа объектов:

```text
A: живёт 10 µs
B: живёт 1 second
C: живёт 1 hour
```

A:

```text
много мусора
высокий allocation churn
```

C:

```text
увеличивает live set
долго остаётся частью heap graph
```

Поэтому GC cost зависит не только от общего числа allocated bytes.

---

# Блок 8. Откуда появляются allocations

## 32. `make`

Например:

```go
buf := make([]byte, 4096)
```

Backing array где-то должен храниться.

Compiler может в отдельных случаях разместить fixed-size data на stack, но dynamic/larger cases часто требуют heap storage.

---

## 33. Рост slice

```go
var xs []int

for i := 0; i < 1_000_000; i++ {
    xs = append(xs, i)
}
```

Когда capacity заканчивается:

```text
старый backing array полон
↓
создаётся больший backing array
↓
данные копируются
↓
slice указывает на новый array
```

Это может породить несколько allocations и copies.

---

## 34. Почему capacity иногда важен

Если размер примерно известен:

```go
xs := make([]int, 0, expected)
```

можно уменьшить число growth reallocations.

Но правило не:

```text
всегда preallocate максимально
```

Слишком большой capacity может:

```text
удерживать лишнюю память
увеличить RSS
```

Снова trade-off.

---

## 35. Strings и `[]byte`

Преобразования:

```go
[]byte(s)
string(b)
```

могут требовать нового storage/copy.

Compiler имеет оптимизации для отдельных случаев, поэтому нельзя утверждать:

```text
каждая conversion всегда allocation
```

Но в hot path это известный источник temporary memory, который надо измерять.

---

## 36. `fmt`

Удобные formatting APIs могут использовать reflection/interface machinery и temporary storage.

Например:

```go
fmt.Sprintf("%s:%d", name, id)
```

В cold path это нормально.

В extremely hot loop может оказаться заметно.

Вывод:

```text
удобство
≠
запрет

горячий path
→
измеряем
```

---

## 37. Interfaces

Передача значения через interface не означает автоматически heap allocation.

Но конкретная interface conversion и дальнейший escape могут приводить к allocations.

Поэтому плохое правило:

```text
interface всегда allocates
```

Правильное:

```text
конкретный call path
↓
benchmark
↓
escape report
```

---

## 38. Closures

Closure может быть дешёвой.

Но если captured environment должен жить дольше frame:

```text
captured values
↓
extended lifetime
```

может появиться heap allocation.

---

## 39. Maps

```go
m := make(map[string]User)
```

Map требует runtime storage.

По мере роста map получает дополнительные backing structures.

Если заранее известна приблизительная cardinality:

```go
make(map[string]User, expected)
```

можно уменьшить часть growth overhead.

Опять же — измеряем.

---

## 40. JSON

Типичный backend hotspot:

```go
data, err := json.Marshal(response)
```

Вокруг serialization могут появляться:

```text
temporary buffers
strings
reflection paths
copies
DTO allocations
```

Один request выглядит безобидно.

При:

```text
20 allocations/request
×
30 000 RPS
=
600 000 allocations/sec
```

ситуация уже другая.

---

# Блок 9. Измеряем allocations

## 41. Benchmark

```go
func BenchmarkEncode(b *testing.B) {
    for b.Loop() {
        encode(user)
    }
}
```

Запуск:

```bash
go test -bench=. -benchmem
```

Вывод может содержать:

```text
850 ns/op
512 B/op
7 allocs/op
```

---

## 42. Что означает `B/op`

```text
B/op
```

сколько bytes в среднем allocated на одну benchmark operation.

Важно:

это не:

```text
сколько memory осталось live после operation
```

Это allocation traffic.

---

## 43. Что означает `allocs/op`

```text
allocs/op
```

сколько heap allocations приходится в среднем на operation.

Например:

```text
100 B/op
10 allocs/op
```

и:

```text
100 B/op
1 alloc/op
```

имеют одинаковые bytes, но разное число allocator events.

---

## 44. Повторяем измерения

Полезно:

```bash
go test -bench=. -benchmem -count=5
```

Серьёзные regression comparisons лучше делать по нескольким запускам.

Главный принцип:

```text
до
↓
изменение
↓
после
```

---

## 45. Нулевые allocations — не самоцель

Административный endpoint:

```text
1 request/hour
```

имеет 5 allocations.

А parsing function на:

```text
100 000 RPS
```

имеет одну лишнюю allocation.

Оптимизировать надо второе.

Performance engineering начинается с:

```text
frequency × cost
```

---

# Блок 10. Pointers и lifetime

## 46. Pointer — reference на storage

```go
x := 42
p := &x
```

Но для GC важнее object graph:

```text
Request
   │
   ▼
User
   │
   ▼
Session
   │
   ▼
Token
```

Pointers связывают objects.

---

## 47. Reachability

GC задаёт вопрос:

> Можно ли добраться до object из roots?

Например:

```text
root
 │
 ▼
Request
 │
 ▼
User
 │
 ▼
Session
```

Все объекты reachable.

Если root reference исчезла, цепочка может стать unreachable.

---

## 48. Reachable не означает «нужен бизнесу»

```go
var cache = map[string][]byte{}
```

Приложение бесконечно добавляет:

```go
cache[id] = hugeData
```

С точки зрения бизнес-логики старые entries давно не нужны.

Но GC видит:

```text
global variable
↓
map
↓
entry
↓
[]byte
```

Object reachable.

Следовательно:

```text
GC не имеет права его удалить
```

Это memory leak на уровне приложения при исправно работающем GC.

---

# Блок 11. Зачем Garbage Collector

## 49. Без автоматического reclaim

Manual memory management:

```text
allocate
↓
use
↓
кто-то обязан free
```

Ошибки:

```text
забыли free → leak
free слишком рано → use-after-free
free дважды → corruption
```

Go использует managed storage.

Для heap values runtime определяет, когда memory становится недостижима и может быть переиспользована.

---

## 50. GC не знает бизнес-смысл

Точная формулировка:

> Collector ищет unreachable managed objects.

Он не знает:

```text
TTL cache entry
старый request
"мы уже никогда это не прочитаем"
```

Если reference существует — object жив для GC.

---

# Блок 12. Tracing GC

## 51. Graph traversal

Учебно:

```text
roots
 │
 ├── A
 │   └── C
 │
 └── B
     └── D

E
F
```

Collector проходит:

```text
roots → A → C
roots → B → D
```

`E` и `F` недостижимы.

---

## 52. Mark

Первая задача:

```text
найти reachable objects
```

Упрощённо:

```text
root
↓
mark object
↓
scan its pointers
↓
mark referenced objects
↓
повторять
```

---

## 53. Sweep

После mark:

```text
marked = live
unmarked = garbage
```

Но allocator ещё нужно сообщить, какую memory можно снова использовать.

Sweep отвечает:

```text
какие места снова free?
```

---

## 54. Tri-color model

Для объяснения tracing удобно:

```text
white
объект ещё не обнаружен

grey
обнаружен, но pointers ещё нужно обработать

black
обнаружен и обработан
```

Это учебная модель.

Не надо преподавать её как:

> «У каждого Go-object физически лежит поле color».

---

# Блок 13. Concurrent GC

## 55. Наивный Stop-The-World collector

```text
application runs
↓
STOP EVERYTHING
↓
scan entire heap
↓
free garbage
↓
START
```

Если heap большой:

```text
pause растёт
```

Для backend:

```text
requests не обслуживаются
↓
latency spike
```

Поэтому Go выполняет основную mark work concurrent с application.

---

## 56. Concurrent означает одновременность

Во время GC:

```text
application goroutines
        +
GC workers
```

работают одновременно.

Например:

```text
CPU0 → request
CPU1 → request
CPU2 → GC
CPU3 → request
```

---

## 57. Concurrent GC не бесплатный

Цена распределена:

```text
background GC CPU
write barrier cost
stack scanning
mutator assists
short STW phases
cache/memory bandwidth pressure
```

Поэтому:

```text
pause маленькая
```

ещё не значит:

```text
GC дешёвый
```

---

# Блок 14. Write barrier

## 58. Проблема

GC уже просканировал:

```text
A
```

Application делает:

```text
A.child = C
```

Heap graph изменился во время collection.

Если collector ничего не узнает, reachable object можно потерять.

---

## 59. Решение

Во время concurrent marking определённые pointer writes сопровождаются дополнительной runtime bookkeeping.

Это write barrier.

Для основной лекции достаточно:

```text
application меняет heap graph
↓
barrier помогает GC не потерять references
```

Как устроен hybrid barrier — отдельная дополнительная лекция.

---

# Блок 15. Green Tea

## 60. Версионное примечание

Начиная с Go 1.26 Green Tea является default garbage collector стандартного toolchain.

Для основной лекции не нужно разбирать его внутренние queues.

Важно понять, какую проблему он решает.

---

## 61. Проблема pointer chasing

Tracing object graph может выглядеть так:

```text
A → object далеко в memory
↓
B → ещё дальше
↓
C → другая page
```

CPU caches работают хуже.

Green Tea лучше группирует marking/scanning work по locality.

Практический смысл:

```text
меньше GC overhead на многих workloads
```

Но programming model остаётся:

```text
reachable → live
unreachable → collectible
```

---

# Блок 16. GC cycle

## 62. Timeline

Упрощённо:

```text
application
    │
    ▼
GC trigger
    │
    ▼
short STW preparation
    │
    ▼
concurrent mark
    │
    ├── GC workers
    ├── stack scanning
    ├── write barriers
    └── assists
    │
    ▼
mark termination
    │
    ▼
short STW
    │
    ▼
sweep / reuse
```

GC cycle — это период работы collector, а не одна pause.

---

## 63. Почему GC запускается

Heap растёт.

Runtime имеет target:

```text
heap goal
```

Collector должен стартовать достаточно рано, чтобы закончить работу до чрезмерного роста heap.

Этим занимается GC pacer.

В основной лекции достаточно идеи:

```text
allocation rate ↑
↓
collector должен начинать раньше / работать активнее
```

---

# Блок 17. GOGC

## 64. Что регулирует GOGC

`GOGC` задаёт относительный growth budget.

Default:

```text
GOGC=100
```

Учебное приближение:

```text
new goal
≈
live heap
+
live heap × GOGC / 100
```

---

## 65. Более точная модель

Современный GC учитывает также root scan work.

Можно думать так:

```text
Target ≈
Live heap
+
(Live heap + GC roots) × GOGC / 100
```

Это модель, а не точная гарантия runtime.

На target также влияют:

```text
GOMEMLIMIT
pacer
runtime overhead
actual workload
```

---

## 66. Пример GOGC=100

Пусть после GC:

```text
live heap = 500 MB
```

Игнорируя roots и поправки:

```text
goal ≈ 1 GB
```

Runtime получает примерно 500 MB growth space.

---

## 67. Низкий GOGC

```text
GOGC=50
```

Следствие:

```text
GC чаще
↓
heap меньше
↓
GC CPU обычно выше
```

Trade-off:

```text
memory ↓
CPU ↑
```

---

## 68. Высокий GOGC

```text
GOGC=200
```

Следствие:

```text
GC реже
↓
heap больше
↓
GC CPU обычно ниже
```

Trade-off:

```text
memory ↑
CPU ↓
```

---

## 69. Почему огромный GOGC не универсальное решение

Да, GC может стать реже.

Но heap получает больше пространства для роста.

В container:

```text
memory limit = 1 GiB
```

это может приблизить OOM.

Именно поэтому нужен абсолютный memory budget.

---

# Блок 18. GOMEMLIMIT

## 70. Проблема абсолютного budget

Пусть:

```text
pod memory limit = 512 MiB
live heap = 350 MiB
```

При `GOGC=100` относительный target без ограничения может захотеть больше memory, чем container реально имеет.

---

## 71. Что делает GOMEMLIMIT

Например:

```bash
GOMEMLIMIT=450MiB
```

Runtime получает soft memory budget.

Важно:

```text
GOMEMLIMIT
≠
максимальный HeapAlloc
```

Он относится к более широкому набору memory, управляемой runtime.

Полезная модель:

```text
Go controlled memory
≈
/memory/classes/total:bytes
-
/memory/classes/heap/released:bytes
```

---

## 72. Почему limit soft

Представим:

```text
GOMEMLIMIT = 500 MB
live set = 490 MB
```

Application всё ещё должна:

```text
обрабатывать requests
создавать temporary objects
завершать GC
```

Если запретить даже временно выйти выше limit, можно получить почти непрерывный GC.

Поэтому limit soft.

---

## 73. Memory limit может увеличить CPU

```text
memory budget ↓
↓
heap goal ↓
↓
GC frequency ↑
↓
GC CPU ↑
↓
request CPU available ↓
↓
latency ↑
```

Главный production-вывод:

> Memory configuration может проявиться как CPU problem.

---

## 74. GOMEMLIMIT и container hard limit

Плохо механически делать:

```text
container hard limit = 1 GiB
GOMEMLIMIT          = 1 GiB
```

У процесса может быть memory вне прямого контроля GC:

```text
cgo/native allocations
some mmap
thread/runtime overhead
external memory
```

Нужен safety headroom.

Конкретное значение подбирается под workload.

---

## Что важно запомнить

1. `GOGC` задаёт относительный CPU ↔ memory trade-off.
2. `GOMEMLIMIT` задаёт soft absolute runtime memory budget.
3. Маленький memory budget может увеличить GC CPU.
4. Container hard limit остаётся внешней границей.
5. GC tuning без понимания allocation rate часто лечит симптом.


---

# Блок 19. GC pauses

## 75. Что такое pause

Есть моменты GC cycle, когда runtime кратко останавливает application world для действий, которым нужно глобально согласованное состояние.

Это STW pause.

Но основная mark work выполняется concurrent.

Поэтому:

```text
длительность GC cycle
≠
длительность STW pause
```

---

## 76. Почему нельзя смотреть только pause duration

Допустим:

```text
STW pause = 150 µs
```

Очень мало.

Но одновременно:

```text
GC использует 25% CPU
```

и request goroutines выполняют assists.

Сервис всё равно может терять throughput и увеличивать p99.

---

## 77. Полная стоимость GC

Нужно смотреть минимум:

```text
GC frequency
GC CPU
allocation rate
live heap
heap goal
scannable heap
assist CPU
pause duration
```

Одна метрика редко отвечает на весь вопрос.

---

# Блок 20. Mutator assists

## 78. Что происходит, если application слишком быстро allocates

Collector должен успевать за heap growth.

Пусть application резко увеличила allocation rate:

```text
500 MB/s
↓
5 GB/s
```

Background GC workers могут не успеть.

Runtime не может просто позволить heap расти бесконечно.

---

## 79. Request goroutine может помочь GC

Allocating goroutine получает GC work debt.

Учебно:

```text
handler
↓
allocation
↓
runtime:
"collector отстаёт"
↓
goroutine выполняет часть marking
↓
возвращается к handler
```

Это mark assist.

---

## 80. Почему assists важны для latency

```text
HTTP request
↓
build response
↓
lots of allocations
↓
GC assist
↓
handler выполняется дольше
```

Request latency выросла.

Но:

```text
STW pause всё ещё маленькая
```

Именно поэтому GC impact нельзя диагностировать только pauses.

---

# Блок 21. Memory pressure

## 81. Что такое memory pressure в backend

Сервис приближается к memory budget:

```text
live objects ↑
temporary allocations ↑
goroutine stacks ↑
runtime memory ↑
```

Runtime получает всё меньше пространства для роста heap.

---

## 82. Типичная цепочка

```text
memory pressure
↓
GC cycles чаще
↓
GC CPU выше
↓
assists больше
↓
application CPU меньше
↓
latency выше
↓
throughput ниже
```

После этого может появиться feedback loop.

---

## 83. Под нагрузкой lifetime тоже меняется

Это очень важная production-связь.

Пусть обычно request живёт:

```text
20 ms
```

При перегрузке:

```text
queueing
DB wait
network wait
↓
request живёт 2 s
```

Значит связанные objects:

```text
DTO
buffers
context
response state
```

живут дольше.

Даже без изменения кода live heap может вырасти только потому, что выросла latency.

Получаем:

```text
latency ↑
↓
object lifetime ↑
↓
live heap ↑
↓
GC pressure ↑
↓
latency ещё ↑
```

---

# Блок 22. Heap ≠ RSS

## 84. Что видит Go runtime

Runtime может разделять memory на классы:

```text
heap objects
heap free
heap released
stacks
runtime metadata
```

---

## 85. Что видит OS

OS имеет свои представления:

```text
virtual memory
RSS
page mappings
```

Это другой уровень abstraction.

---

## 86. Почему VSS может быть огромным

Runtime может резервировать большой virtual address space.

Reserved address space:

```text
не равно физически занятая RAM
```

Поэтому VSS часто плохо отвечает на вопрос:

> Сколько реальной памяти сейчас потребляет service?

Для production полезнее смотреть:

```text
RSS / cgroup usage
+
Go runtime memory metrics
```

---

## 87. Почему HeapAlloc упал, а RSS нет

Было:

```text
HeapAlloc = 2 GB
RSS       = 2.5 GB
```

После collection:

```text
HeapAlloc = 700 MB
RSS       = 2.2 GB
```

Это не обязательно leak.

Возможный lifecycle:

```text
objects dead
↓
memory free для Go allocator
↓
runtime может быстро reuse memory
↓
часть memory ещё не released OS
```

---

# Блок 23. Scavenger — только нужная основа

## 88. Sweep и OS reclaim — разные процессы

GC/sweep могут сделать memory:

```text
free для Go
```

Но это не значит:

```text
physical pages сразу возвращены OS
```

Scavenger помогает runtime release unused physical memory underlying OS.

---

## 89. Почему runtime не отдаёт всё немедленно

Пусть workload пилообразный:

```text
peak 4 GB
↓
idle 1 GB
↓
через минуту снова peak 4 GB
```

Слишком агрессивный release даст:

```text
release
↓
снова acquire/page fault
↓
лишняя kernel work
```

Runtime балансирует:

```text
reuse speed
↔
RSS
```

Internals scavenger — тема дополнительной лекции.

---

# Блок 24. Memory leak в языке с GC

## 90. Что практически называть leak

Полезная engineering-модель:

> Memory остаётся reachable/retained существенно дольше, чем требуется приложению, и usage неконтролируемо растёт.

GC при этом может работать абсолютно правильно.

---

## 91. Unbounded cache

```go
var cache = map[string][]byte{}

func put(key string, data []byte) {
    cache[key] = data
}
```

Если entries никогда не удаляются:

```text
global root
↓
map
↓
all entries
```

GC ничего не сможет сделать.

---

## 92. Slice удерживает большой backing array

```go
big := make([]byte, 100<<20)

small := big[:10]
return small
```

Логически возвращаем:

```text
10 bytes
```

Но `small` продолжает ссылаться на тот же backing array.

В результате:

```text
10-byte view
↓
100 MB backing array retained
```

Иногда имеет смысл скопировать маленький фрагмент в отдельный compact buffer.

Но только если retention действительно проблема.

---

## 93. Queue backlog

Producer быстрее consumer:

```text
producer
>>>>>>>>>>>>>
queue
>>>>>
consumer
```

Queue size:

```text
1k
10k
100k
1M
```

Каждый queued element reachable.

GC ничего не удаляет.

Это не «GC leak».

Это backpressure problem, проявившаяся ростом memory.

---

## 94. Goroutine leak

```go
go func() {
    result := <-ch
    process(result)
}()
```

Если никто никогда не отправит:

```text
goroutine blocked forever
```

Она удерживает:

```text
stack
runtime metadata
references from stack
```

Поэтому:

```text
goroutine leak
↓
memory retention
```

---

## 95. Timer / ticker / lifecycle problems

Long-lived timers, tickers и background goroutines при неправильном lifecycle могут удерживать references и resources дольше ожидаемого.

Полезные вопросы:

```text
кто владелец?
кто завершает?
когда reference исчезает?
как работает cancellation?
```

---

## Что важно запомнить

1. GC удаляет unreachable objects, а не «ненужные бизнесу».
2. Cache, queue и goroutine leaks обычно сохраняют reachability.
3. Маленький slice способен удерживать большой backing array.
4. Backpressure может выглядеть как memory leak.
5. Lifetime ownership важнее самого наличия GC.

---

# Блок 25. Зачем нужен pprof

## 96. До pprof у нас только симптомы

Например:

```text
CPU = 90%
RSS = 1.7 GB
p99 = 1.2 s
goroutines = 25 000
```

Из этих цифр нельзя честно сделать вывод:

```text
проблема GC
```

или:

```text
проблема JSON
```

или:

```text
goroutine leak
```

Нужны profiles.

---

## 97. Что делает profiler

Profiler связывает resource consumption с code paths.

Вместо:

```text
CPU высокий
```

получаем:

```text
какие функции потребляют CPU
```

Вместо:

```text
memory высокая
```

получаем:

```text
какие allocation sites создавали или удерживают memory
```

Вместо:

```text
goroutines много
```

получаем:

```text
где они находятся и на чём ждут
```

---

# Блок 26. Подключаем `net/http/pprof`

## 98. Простой diagnostic server

Можно подключить handlers и поднять отдельный diagnostic listener.

Например:

```go
package main

import (
    "log"
    "net/http"
    _ "net/http/pprof"
)

func startDiagnostics() {
    go func() {
        log.Println(http.ListenAndServe("127.0.0.1:6060", nil))
    }()
}
```

При использовании default mux становятся доступны endpoints:

```text
/debug/pprof/
/debug/pprof/profile
/debug/pprof/heap
/debug/pprof/goroutine
...
```

---

## 99. Почему лучше отдельный diagnostic server

Не стоит смешивать:

```text
public API
```

и:

```text
runtime diagnostics
```

Полезная схема:

```text
:8080
public HTTP

127.0.0.1:6060
diagnostic HTTP
```

или internal-only network endpoint.

---

## 100. Почему pprof нельзя бездумно публиковать наружу

Profiles могут раскрывать:

```text
function names
package structure
stack traces
runtime behavior
internal call paths
```

Кроме того, collection некоторых profiles имеет overhead.

Production access должен быть ограничен.

---

# Блок 27. CPU profile

## 101. Главный вопрос CPU profile

> Где process проводит время, когда реально использует CPU?

Снять 30 секунд:

```bash
go tool pprof \
  http://localhost:6060/debug/pprof/profile?seconds=30
```

CPU profile собирается за временной интервал.

---

## 102. `top`

Внутри pprof:

```text
(pprof) top
```

Условный вывод:

```text
flat      flat%   sum%      cum
2.5s      25%     25%       3.0s   encoding/json...
1.8s      18%     43%       1.8s   runtime...
...
```

---

## 103. Flat

`flat` отвечает:

> Сколько CPU samples попало непосредственно внутрь функции?

Например:

```text
parseJSON:
flat = 20%
```

Значит собственный body функции CPU-heavy.

---

## 104. Cum

`cum`:

> Функция + все функции, которые она вызвала.

Например:

```text
Handler
  ↓
BuildResponse
  ↓
json.Marshal
  ↓
encoding internals
```

`Handler` может иметь:

```text
flat = 1%
cum = 60%
```

Handler сам почти ничего тяжёлого не делает, но ведёт в expensive subtree.

---

## 105. `top -cum`

```text
(pprof) top -cum
```

Полезен, когда хотим понять:

```text
кто приводит к дорогой работе
```

а не только:

```text
где лежат leaf samples
```

---

## 106. `list`

```text
(pprof) list FunctionName
```

Связывает samples с конкретными source lines.

Цель profiling:

```text
package
↓
function
↓
call path
↓
конкретная строка
```

---

## 107. Graph и flame graph

Визуализация помогает видеть call relationships.

Читать её надо как:

```text
какая call path ведёт к cost?
```

А не:

> «самый широкий прямоугольник плохой сам по себе».

---

# Блок 28. Чего CPU profile не показывает

## 108. Wait не является CPU

```go
rows, err := db.QueryContext(ctx, query)
```

PostgreSQL отвечает 3 секунды.

Goroutine ждёт network/DB.

Process может почти не использовать CPU.

CPU profile ничего драматического не покажет.

---

## 109. Slow endpoint ≠ CPU bottleneck

```text
p99 = 5 s
CPU = 20%
```

Вероятные направления:

```text
DB wait
network wait
lock contention
queueing
connection pool wait
blocking
```

CPU profile — неправильный единственный инструмент.

---

## 110. Другие диагностические инструменты

Для ожиданий и contention нужны:

```text
goroutine profile
block profile
mutex profile
execution trace
DB metrics
distributed tracing
```

В этой лекции подробно разбираем CPU/heap/goroutine.

---

# Блок 29. CPU profile и GC

## 111. Что может встретиться

Например runtime-related symbols:

```text
runtime.mallocgc
runtime.gcBgMarkWorker
runtime.gcAssistAlloc
runtime.scanobject
```

Конкретные имена — implementation details.

Они нужны не для заучивания, а для построения гипотезы.

---

## 112. `mallocgc`

Большой cumulative cost вокруг allocation path может означать:

```text
application очень много allocates
```

Следующий шаг:

```text
alloc profile
benchmark
escape analysis
```

---

## 113. `gcAssistAlloc`

Если request goroutines много времени проводят в assist path:

```text
collector не успевает только background work
```

Возможные причины:

```text
allocation pressure
tight memory budget
large scan work
```

---

## 114. `gcBgMarkWorker`

Много background GC work означает:

```text
collector реально потребляет CPU
```

Но нужно учитывать:

```text
dedicated work
idle work
overall CPU saturation
```

Часть GC может использовать CPU capacity, которая иначе простаивала бы.

---

# Блок 30. Heap profile

## 115. Два разных memory-вопроса

Memory problem часто сводится к одному из двух:

```text
кто сейчас удерживает memory?
```

или:

```text
кто создаёт огромный allocation traffic?
```

Heap/alloc profiles позволяют различать их.

---

## 116. Снять heap profile

```bash
go tool pprof \
  http://localhost:6060/debug/pprof/heap
```

Heap profile использует sampling.

Это статистическое представление memory, а не запись буквально каждой allocation.

---

## 117. `inuse_space`

Вопрос:

> Какие call sites удерживают больше всего sampled live bytes сейчас?

Полезно для:

```text
memory leak
retention
large live heap
```

---

## 118. `inuse_objects`

Вопрос:

> Кто удерживает больше всего live objects?

Иногда bytes умеренные, но objects миллионы.

Это тоже может увеличивать GC work.

---

## 119. `alloc_space`

Вопрос:

> Кто исторически выделил больше всего bytes?

Полезно для поиска allocation churn и GC pressure.

---

## 120. `alloc_objects`

Вопрос:

> Кто породил больше всего objects?

Например миллионы tiny short-lived objects могут быть существенны даже при небольшом live heap.

---

# Блок 31. Leak vs churn

## 121. Сервис A — churn

```text
live heap = 150 MB
alloc rate = 6 GB/s
```

Heap стабилен.

CPU profile показывает allocation/GC.

`alloc_space` показывает:

```text
JSON conversion
temporary buffers
DTO creation
```

Это allocation pressure.

---

## 122. Сервис B — retention

```text
live heap:
300 MB
500 MB
800 MB
1.4 GB
```

`inuse_space` показывает:

```text
cache
queue
large slice backing arrays
```

Это retained/live memory problem.

---

## 123. Почему один heap snapshot недостаточен

Для leak полезно сравнивать во времени:

```text
profile t0
profile t1
profile t2
```

Нас интересует:

```text
что систематически растёт?
```

А не просто:

> «что было самым большим в одном snapshot».


---

# Блок 32. Goroutine profile

## 124. Что он показывает

Goroutine profile показывает stack traces текущих goroutines.

Снять:

```bash
go tool pprof \
  http://localhost:6060/debug/pprof/goroutine
```

Или посмотреть debug output соответствующего endpoint.

---

## 125. Почему count сам по себе недостаточен

```text
goroutines = 20 000
```

Это может быть:

```text
нормально при 20 000 active connections
```

или:

```text
катастрофический leak
```

Нужно знать:

```text
где они стоят?
на чём ждут?
растёт ли count?
есть ли одинаковый dominant stack?
```

---

## 126. Типичная картина goroutine leak

Например:

```text
15 000 goroutines
↓
same stack
↓
waiting on channel receive
```

или:

```text
same stack
↓
blocked on channel send
```

Это сильный сигнал:

```text
lifecycle producer/consumer сломан
```

---

## 127. Connection pool pressure

Допустим много goroutines находятся вокруг `database/sql` и ждут connection.

Это не обязательно leak.

Возможные причины:

```text
pool слишком мал
queries стали медленнее
DB деградирует
слишком высокая concurrency
transactions держат connections слишком долго
```

Profile показывает место ожидания.

Root cause ещё надо расследовать.

---

# Блок 33. Go 1.27: `goroutineleak` profile

## 128. Новая возможность

В Go 1.27 generally available отдельный profile:

```text
goroutineleak
```

Endpoint:

```text
/debug/pprof/goroutineleak
```

Он использует reachability information runtime/GC, чтобы находить определённый класс goroutines, permanently blocked на поддерживаемых concurrency primitives и не способных разблокироваться.

---

## 129. Чем он отличается от обычного goroutine profile

Обычный profile:

```text
покажи все goroutines
```

Leak profile:

```text
автоматически выдели определяемый runtime класс permanent leaks
```

Это уменьшает объём ручного анализа.

---

## 130. Ограничения

Это не oracle.

Не все возможные goroutine leaks можно доказать автоматически.

Например goroutines, blocked на:

```text
network I/O
file I/O
custom synchronization
```

могут не попадать в этот profile.

Кроме того, globally reachable synchronization primitive может мешать доказать permanent leak.

Следовательно:

```text
goroutineleak empty
≠
доказательство отсутствия всех goroutine leaks
```

---

# Блок 34. `runtime/metrics` как приборная панель

## 131. Heap state

Полезные metrics:

```text
/gc/heap/live:bytes
/gc/heap/goal:bytes
/gc/heap/allocs:bytes
/gc/heap/frees:bytes
```

Вопросы:

```text
live растёт?
goal далеко от live?
какой allocation traffic?
```

---

## 132. Scan work

```text
/gc/scan/heap:bytes
/gc/scan/stack:bytes
```

Помогают отличить:

```text
много raw bytes
```

от:

```text
много scannable pointer-rich memory
```

И увидеть стоимость огромного числа goroutine stacks.

---

## 133. GC CPU classes

```text
/cpu/classes/gc/mark/assist:cpu-seconds
/cpu/classes/gc/mark/dedicated:cpu-seconds
/cpu/classes/gc/mark/idle:cpu-seconds
/cpu/classes/gc/total:cpu-seconds
```

Они помогают ответить:

```text
GC выполнялся background workers?
requests сами помогали через assists?
GC использовал idle CPU?
```

---

## 134. Memory classes

```text
/memory/classes/heap/objects:bytes
/memory/classes/heap/free:bytes
/memory/classes/heap/released:bytes
```

Это полезно для вопроса:

```text
HeapAlloc упал,
а RSS почему нет?
```

---

# Блок 35. pprof как расследование

## 135. Неправильный процесс

```text
CPU высокий
↓
открыли flame graph
↓
увидели runtime
↓
"GC плохой"
```

Это угадывание.

---

## 136. Правильный процесс

```text
симптом
↓
базовые metrics
↓
гипотеза
↓
правильный profile
↓
репрезентативная нагрузка
↓
call path
↓
source
↓
изменение
↓
повторное measurement
```

---

## 137. CPU высокий

Первый шаг:

```text
CPU profile
```

Если hotspot — application algorithm:

```text
оптимизируем algorithm
```

Если hotspot — allocation/GC:

```text
alloc profile
↓
benchmark
↓
escape analysis
```

---

## 138. RSS высокий

Смотрим:

```text
Go memory metrics
heap inuse profile
RSS/cgroup
```

И задаём вопросы:

```text
live objects?
free Go heap?
released memory?
external/native memory?
```

---

## 139. Goroutines растут

Смотрим:

```text
goroutine count trend
goroutine profile
goroutineleak profile
```

Группируем stacks.

Ищем dominant wait point.

---

# Блок 36. Production-кейс №1: allocation churn

## 140. Симптом

```text
RPS = 20 000
CPU = 92%
RSS = 700 MB
live heap = 250 MB
p99 = 800 ms
```

Memory выглядит не ужасно.

Но GC CPU высокий.

---

## 141. CPU profile

Видим:

```text
json encoding
runtime allocation
GC assist paths
```

Гипотеза:

```text
application создаёт слишком много temporary objects
```

---

## 142. Alloc profile

`alloc_space`:

```text
Handler
↓
BuildResponse
↓
Transform
↓
json.Marshal
```

Benchmark:

```text
40 allocs/op
18 KB/op
```

---

## 143. Исправление

Например:

```text
убрали промежуточный DTO copy
заранее выделили разумную slice capacity
убрали лишнюю string/[]byte conversion
```

После:

```text
18 allocs/op
7 KB/op
```

---

## 144. Результат

```text
allocation rate ↓
↓
GC work ↓
↓
assist CPU ↓
↓
CPU ↓
↓
p99 ↓
```

Главный урок:

> GC bottleneck часто начинается не с настройки collector, а с allocation pattern приложения.

---

# Блок 37. Production-кейс №2: memory retention

## 145. Симптом

```text
live heap:
500 MB
700 MB
950 MB
1.3 GB

goroutine count:
stable
```

Heap растёт независимо от стабилизировавшегося traffic.

---

## 146. Heap profile

`inuse_space` показывает:

```text
cache.Put
↓
map assignment
↓
large []byte
```

Cache не имеет eviction.

---

## 147. Почему GC не спасает

```text
global cache
↓
entries
↓
buffers
```

Все reachable.

Collector делает именно то, что обязан:

```text
не удаляет живые objects
```

Root cause — ownership/lifecycle cache data.

---

# Блок 38. Production-кейс №3: goroutine leak

## 148. Симптом

```text
goroutines:
500
2 000
8 000
30 000

RSS растёт
CPU постепенно растёт
```

---

## 149. Goroutine profile

Большинство:

```text
worker.func1
↓
ch <- result
```

Receiver может уйти раньше по error path.

Senders остаются blocked.

---

## 150. Почему это становится memory problem

Каждая leaked goroutine:

```text
stack
+
metadata
+
references
```

Итог:

```text
goroutine leak
↓
more retained references
↓
live memory ↑
↓
GC scan/root work ↑
```

Concurrency bug превратился в memory и CPU problem.

---

# Блок 39. Production-кейс №4: слишком маленький GOMEMLIMIT

## 151. Симптом

После уменьшения pod memory:

```text
RSS стабилизировался ниже
CPU вырос с 45% до 85%
p99 вырос в 2 раза
```

Heap leak отсутствует.

---

## 152. Смотрим GC

```text
GC cycles/sec ↑
heap goal близко к live heap
assist CPU ↑
```

Причина:

```text
runtime почти не имеет growth runway
```

---

## 153. Причинная цепочка

```text
memory budget слишком tight
↓
collector запускается часто
↓
goroutines чаще помогают GC
↓
CPU на requests меньше
↓
latency выше
```

Это следствие resource configuration.

---

# Блок 40. Production-кейс №5: RSS не падает

## 154. Симптом

Traffic spike закончился:

```text
HeapAlloc:
3 GB → 800 MB

RSS:
3.6 GB → 3.2 GB
```

Команда говорит:

> leak.

---

## 155. Проверяем layers

Смотрим:

```text
heap objects
heap free
heap released
RSS
```

Если objects резко упали, а free heap вырос:

```text
GC уже освободил objects для allocator
```

Если released memory постепенно растёт:

```text
runtime возвращает часть memory OS
```

RSS не обязан синхронно повторять HeapAlloc.

---

# Блок 41. Практическая мини-демонстрация

## 156. Этап 1 — увидеть escape

```go
package main

type User struct {
    ID int
}

func value() User {
    return User{ID: 42}
}

func pointer() *User {
    u := User{ID: 42}
    return &u
}

func main() {
    _ = value()
    _ = pointer()
}
```

Запуск:

```bash
go build -gcflags="-m=2" .
```

Разбираем:

```text
что compiler смог оставить локально?
что escape'ится?
почему?
```

---

## 157. Этап 2 — увидеть allocations benchmark'ом

Добавить benchmark.

```bash
go test -bench=. -benchmem
```

Смотреть:

```text
B/op
allocs/op
```

Важно заставить benchmark реально использовать результат, чтобы compiler не удалил бессмысленную работу.

---

## 158. Этап 3 — создать allocation pressure

Сделать handler, специально создающий temporary buffers.

Нагрузить сервис.

Смотреть:

```text
CPU
allocation rate
GC cycles
heap goal
latency
```

---

## 159. Этап 4 — снять CPU profile

```bash
go tool pprof \
  http://localhost:6060/debug/pprof/profile?seconds=30
```

Найти:

```text
application hotspots
allocation/GC paths
```

---

## 160. Этап 5 — сравнить `alloc_space` и `inuse_space`

Студенты должны руками увидеть:

```text
"кто много создаёт"
≠
"кто много удерживает"
```

Это одна из самых важных диагностических мыслей лекции.

---

## 161. Этап 6 — исправить и повторить

Уменьшить ненужные allocations.

Повторить:

```text
benchmark
load test
CPU profile
heap profile
```

Нужна доказанная разница:

```text
до
vs
после
```

---

# Блок 42. Границы основной лекции

## 162. Что студент должен понимать после неё

Он должен уверенно объяснить:

```text
почему value может escape
почему pointer не означает heap
почему allocation rate важен
как работает reachability GC
почему concurrent GC всё равно стоит CPU
что регулируют GOGC и GOMEMLIMIT
почему HeapAlloc и RSS различаются
какой pprof profile выбрать
```

---

## 163. Что оставляем дополнительной лекции

Пока не обязательно подробно разбирать:

```text
mcache
mcentral
mspan internals
size classes подробно
tiny allocator
Green Tea internal queues
hybrid barrier exact invariant
sweepgen
page allocator
madvise details
scavenger internals
```

Основная лекция строит mental model.

Дополнительная отвечает:

> «Хорошо, а как runtime физически всё это делает?»

---

# Что важно запомнить

1. **Stack/heap placement в Go — решение compiler/runtime, а не прямое следствие синтаксиса.**

2. **Главный вопрос — lifetime.** Если value должен пережить текущий frame или compiler не может доказать обратное, может потребоваться heap storage.

3. **Pointer не означает heap. `new(T)` не означает heap. Локальная переменная не гарантирует stack.**

4. **Stack allocation обычно значительно дешевле и не создаёт отдельной GC work.**

5. **Цена heap allocation — не только момент allocation.** Object позже может увеличивать marking, sweeping, memory bandwidth и GC assist work.

6. **Allocation rate и live heap — разные характеристики.** Маленький live heap может сосуществовать с огромным GC churn.

7. **GC смотрит reachability, а не бизнес-смысл.** Поэтому unbounded cache, queue backlog и goroutine leaks создают memory retention при корректном GC.

8. **Go GC выполняет основную mark work concurrent с application, но GC всё равно потребляет CPU.**

9. **Маленькая STW pause не доказывает, что GC дешёвый.** Нужно смотреть total GC CPU, cycles, assists и allocation rate.

10. **`GOGC` регулирует trade-off memory ↔ CPU. `GOMEMLIMIT` задаёт soft memory budget runtime.**

11. **Слишком маленький memory budget может сделать приложение CPU-bound через частый GC и assists.**

12. **Heap memory и RSS — разные уровни.** Освобождение object для Go allocator не означает мгновенный возврат physical pages ОС.

13. **CPU profile отвечает на вопрос «где тратится CPU». Он не объясняет ожидание DB/network автоматически.**

14. **`inuse_space` помогает искать retention. `alloc_space` — allocation churn.**

15. **Goroutine profile показывает все goroutines; Go 1.27 `goroutineleak` дополнительно умеет автоматически находить определённый класс permanently blocked leaks.**

16. **Оптимизация начинается с измерения.** Правильная цепочка: symptom → metrics → profile → source → change → повторное measurement.

---

# Финальная схема лекции

```text
                     SOURCE CODE
                         │
                         ▼
                      values
                         │
                         ▼
                      lifetime
                         │
                         ▼
                 escape analysis
                  │             │
                  ▼             ▼
                stack          heap
                  │             │
          frame завершён        │
                  │             ▼
                  │         allocations
                  │             │
                  │             ▼
                  │         heap growth
                  │             │
                  │             ▼
                  │            GC
                  │       ┌─────┼──────┐
                  │       ▼     ▼      ▼
                  │    workers barrier assists
                  │       └─────┼──────┘
                  │             ▼
                  │         CPU cost
                  │             │
                  │             ▼
                  │      memory pressure
                  │             │
                  │             ▼
                  │        backend latency
                  │
                  └─────────────────────────────┐
                                                ▼
                                             pprof
                                      ┌─────────┼─────────┐
                                      ▼         ▼         ▼
                                     CPU       heap    goroutine
                                      │         │         │
                                      └─────────┼─────────┘
                                                ▼
                                       concrete code path
                                                │
                                                ▼
                                            fix + remeasure
```

Финальный инженерный вывод:

> Memory performance в Go начинается не с GC knobs. Она начинается с lifetime данных и allocation patterns приложения. Runtime может очень быстро выделять memory и эффективно собирать мусор, но под нагрузкой даже маленькие лишние allocations умножаются на RPS, превращаются в GC work, занимают CPU и начинают влиять на tail latency. Поэтому задача backend-разработчика — понимать, какую работу его код заставляет runtime выполнять, измерять эту работу и устранять действительно дорогие места.

---

# Официальные материалы для преподавателя

- Go GC Guide: https://go.dev/doc/gc-guide
- Go diagnostics: https://go.dev/doc/diagnostics
- `net/http/pprof`: https://pkg.go.dev/net/http/pprof
- `runtime/metrics`: https://pkg.go.dev/runtime/metrics
- Green Tea GC: https://go.dev/blog/greenteagc
- Go 1.26 release: https://go.dev/blog/go1.26
- Go 1.27 release notes: https://go.dev/doc/go1.27
- Allocating on the Stack: https://go.dev/blog/allocation-optimizations
- Size-Specialized Memory Allocation: https://go.dev/blog/size-specialized-allocations
- Goroutine Leak Profiles: https://go.dev/blog/goroutine-leak-profiles
