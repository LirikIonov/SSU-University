# Дополнительная лекция 2. Go GC internals и memory allocator

## 1. С чего начинается heap allocation

В основной лекции мы остановились примерно здесь:

```text
variable
   ↓
escape analysis
   ↓
stack или heap
```

Теперь интересует правая ветка.

```go
func newUser() *User {
    u := User{}
    return &u
}
```

Компилятор решил:

```text
u escapes
```

Прекрасно.

Но фраза «объект попал в heap» почти ничего не объясняет.

Возникают вопросы:

- кто ищет свободную память;
- нужен ли mutex на каждую allocation;
- где runtime хранит миллионы маленьких объектов;
- почему `new(User)` не вызывает `mmap`;
- откуда GC знает границы объекта;
- как GC узнаёт, содержит объект pointers или нет;
- что происходит после смерти объекта;
- почему память после GC может не исчезнуть из RSS;
- зачем одновременно существуют GC и scavenger.

Вот здесь начинается настоящий memory runtime Go.

---

## 2. Общая архитектура allocator

Главная цепочка:

```text
goroutine
    │
    ▼
current P
    │
    ▼
mcache
    │
    ▼
mspan
    │
    │ нет свободного места
    ▼
mcentral
    │
    │ нет подходящего span
    ▼
mheap
    │
    │ нужны новые страницы
    ▼
page allocator
    │
    ▼
OS
```

Но очень важно:

```text
allocation ≠ каждый раз mcache → mcentral → mheap → OS
```

Это была бы ужасная система.

Обычный маленький allocation заканчивается уже здесь:

```text
P
↓
mcache
↓
mspan
↓
free slot
```

без обращения к центральному heap allocator и без глобального mutex.

Именно поэтому hierarchy существует.

Runtime исходно был вдохновлён TCMalloc, хотя современная реализация давно существенно от него разошлась. Маленькие allocations обслуживаются через size-segregated per-P structures; объекты до 32 KiB относятся к small allocations.

---

## 3. Почему allocator связан с P

Вот здесь хорошо вернуть scheduler:

```text
G → M → P
```

У каждого `P` есть свой `mcache`.

То есть:

```text
P0 → mcache0
P1 → mcache1
P2 → mcache2
P3 → mcache3
```

`mcache` принадлежит именно P, а не goroutine.

Это принципиально.

Представим 100 000 goroutines.

Мы же не хотим:

```text
100 000 goroutines
→
100 000 allocator caches
```

И не хотим один:

```text
global allocator lock
```

на все allocations.

Получаем промежуточную модель:

```text
тысячи G
    ↓
несколько P
    ↓
несколько mcache
```

Количество allocator hot paths приблизительно связано с реальной параллельностью выполнения.

`mcache` в runtime является per-P cache и поэтому на основном пути маленького allocation не требует locking.

---

## 4. Java-мост: mcache — это не совсем TLAB

Java-разработчик здесь сразу вспоминает:

```text
Thread Local Allocation Buffer
```

И аналогия полезная, но неполная.

Упрощённо:

```text
JVM:
Thread
  ↓
TLAB
  ↓
bump pointer allocation
```

В Go:

```text
P
 ↓
mcache
 ↓
mspan нужного size class
 ↓
free slot
```

Главная разница:

`mcache` — это не просто непрерывный кусок памяти, по которому двигается один allocation pointer.

Он кеширует подходящие `mspan` для разных классов размеров.

То есть внутри условно:

```text
mcache
 ├─ span for 16-byte objects
 ├─ span for 24-byte objects
 ├─ span for 32-byte objects
 ├─ span for 48-byte objects
 ├─ ...
```

---

## 5. Что такое mspan

Вот центральная сущность allocator.

Упрощение:

> `mspan` — кусок heap.

Точнее:

**`mspan` — metadata, описывающая непрерывный run runtime pages, используемый allocator определённым образом.**

Go runtime работает с heap pages размером 8192 байт. Это именно **runtime page**, а не обещание, что physical page ОС тоже 8 KiB. В Go 1.27 `PageShift=13`, то есть runtime page = 8 KiB.

Например:

```text
mspan
│
├── page
├── page
├── page
└── page
```

Для маленьких объектов span обычно предназначен под объекты одного size class.

Например условно:

```text
span
┌──────┬──────┬──────┬──────┬──────┐
│ 64 B │ 64 B │ 64 B │ 64 B │ ...  │
└──────┴──────┴──────┴──────┴──────┘
```

Runtime не хранит внутри одного такого span случайную смесь:

```text
User 37 B
Order 913 B
[]byte 4 KB
Widget 71 B
```

Это сильно усложнило бы поиск свободной памяти и усилило fragmentation.

---

## 6. Что хранится в mspan

Нам не нужно читать весь `struct mspan`, но несколько полей дают почти всю модель allocator.

У него есть примерно такие концепции:

```text
startAddr
npages
elemsize
nelems

allocBits
gcmarkBits

freeindex
allocCount

spanclass
sweepgen
```

`allocBits` отвечает на вопрос:

> Какие object slots сейчас заняты?

`gcmarkBits`:

> Какие объекты GC отметил живыми в текущем цикле?

`freeindex`:

> Откуда начинать искать следующий свободный slot?

`elemsize`:

> Какого размера slot?

`nelems`:

> Сколько slots помещается в span?

Текущий runtime действительно хранит для span allocation bitmap и GC mark bitmap; allocator ищет свободный slot через bitmap начиная с `freeindex`.

Получается:

```text
mspan

slot:      0 1 2 3 4 5 6 7
allocBits: 1 1 0 1 0 1 1 0
                 ↑
              свободно
```

Allocation:

```text
найти 0
↓
поставить 1
↓
вернуть address slot
```

Никакого `malloc()` ОС на каждый объект.

---

## 7. Size classes

Теперь вопрос:

> Что значит «span для объектов одного размера»?

Если объект занимает:

```text
37 bytes
```

runtime не обязан создавать отдельную категорию ровно на 37 байт.

Размер округляется до size class.

В Go 1.27 small allocator имеет 68 записей size classes, включая нулевой class; реальные классы идут от 8 байт до 32 KiB. Например присутствуют 32, 48, 64, 80, 96 байт и так далее.

Условно:

```text
requested = 37 B
       ↓
size class = 48 B
       ↓
slot = 48 B
```

Мы выиграли простоту allocator.

Но получили:

```text
48 - 37 = 11 B
```

внутренней fragmentation.

Вот первый trade-off:

```text
больше size classes
→ меньше waste
→ больше allocator metadata / complexity

меньше size classes
→ allocator проще
→ больше internal fragmentation
```

Go выбирает набор classes как компромисс.

---

## 8. Size class — это ещё не весь spanClass

А вот здесь интересная деталь.

Runtime различает:

```text
pointer-containing objects
```

и

```text
pointer-free objects
```

Для этого `spanClass` кодирует:

```text
size class
+
noscan bit
```

Фактически runtime вычисляет его примерно как:

```text
sizeClass << 1 | noscan
```

`noscan` span содержит объекты без pointers, поэтому GC не должен сканировать их содержимое в поисках следующих heap references.

Сравним:

```go
type A struct {
    X int64
    Y int64
}
```

и:

```go
type B struct {
    X *User
    Y int64
}
```

Пусть оба объекта близки по размеру.

Для GC они принципиально разные.

`A`:

```text
16 bytes
no pointers
↓
GC:
"внутри смотреть нечего"
```

`B`:

```text
pointer
+
integer
↓
GC:
"надо проверить pointer"
```

Это очень важный инженерный вывод:

**стоимость heap определяется не только количеством байтов, но и структурой pointer graph.**

Два heap по 1 GB могут иметь очень разную стоимость GC.

---

## 9. Почему GC любит pointer-free данные

Представим:

```go
[]byte
```

размером 100 MB.

И:

```go
[]*Node
```

размером 100 MB.

Количество памяти одинаковое.

Но GC workload совершенно разный.

Для:

```text
[]byte
```

GC нужно понять, что объект жив.

Содержимое не содержит heap pointers.

Для:

```text
[]*Node
```

нужно пройти pointer slots:

```text
ptr
ptr
ptr
ptr
ptr
...
```

и продолжить graph traversal.

Поэтому:

```text
heap bytes
```

и:

```text
scannable heap bytes
```

— разные величины.

В `runtime/metrics` существует отдельный показатель:

```text
/gc/scan/heap:bytes
```

для scannable heap.

---

## 10. mcache

Теперь раскручиваем hierarchy.

`mcache` — быстрый per-P уровень.

```text
P
│
└── mcache
      │
      ├── mspan class X
      ├── mspan class Y
      ├── mspan class Z
      └── ...
```

При allocation маленького объекта:

```text
1. определить size class
2. определить scan/noscan
3. получить span из mcache
4. найти free slot
5. пометить slot allocated
6. вернуть pointer
```

Это hot path.

Главная инженерная идея:

```text
common case
=
локальные данные P
+
без глобальной блокировки
```

---

## 11. Что происходит, когда mspan заполнен

Допустим:

```text
mcache
 ↓
span for 64 B objects
```

оказался полностью занят.

Теперь нужно refill.

```text
mcache
   ↓
mcentral
```

---

## 12. mcentral

Упрощение:

> `mcentral` — центральный список свободной памяти.

Точнее:

**каждый `mcentral` управляет spans конкретного spanClass.**

Причём сами free objects находятся внутри `mspan`; `mcentral` управляет наборами spans.

Современная реализация отдельно отслеживает partially/full spans и swept/unswept состояния.

Условно:

```text
mcentral[class 64 noscan]

partial:
  span A
  span B
  span C

full:
  span D
  span E
```

`mcache` не просит:

> Дай мне один объект.

Он получает:

> Дай мне span.

Почему?

Потому что lock acquisition тогда амортизируется сразу на множество будущих allocations.

---

## 13. Почему mcentral нельзя использовать на каждый allocation

Представим:

```text
P0 ─┐
P1 ─┼──→ mcentral lock
P2 ─┤
P3 ─┘
```

При высокой allocation rate:

```text
10 000 000 allocations/sec
```

мы получили бы великолепный глобальный synchronization bottleneck.

Поэтому:

```text
mcentral
↓
редкий refill

mcache
↓
тысячи быстрых allocations
```

Это тот же общий паттерн, который мы уже видели в runtime:

```text
local fast path
+
shared slow path
```

---

## 14. mheap

Если `mcentral` не может найти подходящий span:

```text
mcentral
   ↓
mheap
```

`mheap` управляет heap на уровне runs of pages.

```text
objects
   ↓
mspan
   ↓
runtime pages
   ↓
mheap/page allocator
```

То есть уровни абстракции:

```text
object
↓
slot
↓
span
↓
page
↓
OS memory
```

Не надо воспринимать `mheap` как аналог Java heap в смысле «место, где просто лежат все объекты».

Это ещё и управляющая структура runtime для page-level allocation.

---

## 15. Small и large allocation

Go делит allocations по размеру.

Текущая граница small allocation:

```text
≤ 32 KiB
```

Large objects:

```text
> 32 KiB
```

идут непосредственно через heap-level allocation, минуя обычный `mcache → mcentral` small-object path.

Ментальная модель:

```text
small:

malloc
 ↓
mcache
 ↓
mspan
```

и:

```text
large:

malloc
 ↓
mheap
 ↓
pages
```

Почему?

Нет смысла делать size classes:

```text
33 KB
34 KB
35 KB
...
7 MB
```

Для больших объектов проще выдавать необходимое количество pages.

---

## 16. Tiny allocator

А теперь другой конец шкалы.

Представим много escaping объектов:

```go
new(byte)
new(uint16)
new(uint32)
```

Выделять под каждый отдельный крошечный объект полноценный allocator slot может быть дорого.

Поэтому есть tiny allocator.

Для pointer-free объектов меньше 16 байт runtime может объединять несколько allocations внутри одного 16-byte block.

Например:

```text
16-byte tiny block

┌────┬────┬────────┬──────┐
│ 1B │ 2B │   4B   │ ...  │
└────┴────┴────────┴──────┘
```

Но только:

```text
noscan objects
```

Почему?

Потому что совместное размещение pointer-containing объектов резко усложнило бы tracing и metadata.

---

## 17. Цена tiny allocator

У него тоже есть trade-off.

Несколько объектов разделяют один block.

Следовательно:

```text
object A dead
object B alive
```

может означать:

```text
whole tiny block still retained
```

Runtime учитывает это: block освобождается, когда его subobjects больше недостижимы. Текущий размер tiny block — 16 байт.

Мы сэкономили:

```text
allocation metadata
+
allocator work
```

но потенциально удерживаем несколько лишних байт.

---

## 18. mallocgc

Исторически центральная функция heap allocation:

```text
runtime.mallocgc
```

Упрощённая сигнатура:

```go
mallocgc(size, type, needzero)
```

Ей важно знать:

```text
сколько байт?
есть ли pointers?
нужно ли zeroing?
```

Через эту информацию runtime выбирает:

- tiny;
- small scan;
- small noscan;
- large;
- соответствующий size/span class.

`newobject`, например, передаёт тип дальше в allocator.

---

## 19. Но в Go 1.27 allocator уже стал хитрее

Здесь обязательно сделать version note.

Раньше многие compiler-generated heap allocations в итоге проходили через generic `mallocgc`, которому приходилось выполнять проверки:

```text
какой размер?
scan?
noscan?
tiny?
какой class?
```

В Go 1.27 compiler для некоторых маленьких объектов размером менее 80 байт генерирует вызовы size-specialized allocation routines.

Идея:

```text
compiler уже знает:
size = 32
contains pointers = false
```

Зачем снова выяснять это внутри generic allocator?

Специализированный path может пропустить часть branches и indirect work.

Главный вывод студентам:

**runtime internals — движущаяся реализация. Ментальная модель стабильнее имён конкретных функций.**

---

## 20. Полный small-allocation path

Теперь собираем всё:

```text
source code
   ↓
escape analysis
   ↓
compiler generates allocation
   ↓
size / type information
   ↓
small?
   ↓ yes
size class
   ↓
scan / noscan
   ↓
spanClass
   ↓
current P
   ↓
mcache
   ↓
cached mspan
   ↓
free slot?
   ├── yes → allocate
   │
   └── no
        ↓
     mcentral
        ↓
     suitable span?
        ├── yes → return span to mcache
        │
        └── no
             ↓
            mheap
             ↓
          allocate pages
             ↓
         initialize mspan
```

И только если runtime действительно требует больше memory:

```text
mheap
 ↓
OS abstraction
 ↓
mmap / platform mechanism
```

---

## 21. allocator и OS живут на разных масштабах

Вот заблуждение:

> Я выделил 64 байта — Go попросил у Linux 64 байта.

Нет.

ОС выдаёт memory крупнее.

Allocator потом дробит её:

```text
OS memory
   ↓
pages
   ↓
spans
   ↓
slots
   ↓
objects
```

`sysAlloc` обычно получает крупные zeroed regions размером порядка сотен KiB или MiB, а не обслуживает каждый language-level allocation отдельно.

---

## 22. Где allocator встречается с GC

До этого allocator просто отмечал slots занятыми.

Но теперь объект умер.

```text
slot allocated
```

ещё не означает:

```text
slot reusable
```

Сначала GC должен доказать, что объект unreachable.

И тут `mspan` оказывается общей точкой двух подсистем:

```text
allocator
  ↓
allocBits

GC
  ↓
gcmarkBits

        mspan
```

Очень красивый момент архитектуры runtime.

---

## 23. Текущий GC Go

На уровне общей архитектуры Go GC остаётся:

- tracing;
- precise;
- concurrent;
- parallel;
- mark-and-sweep;
- non-generational;
- non-compacting;
- с write barrier.

Разберём слова.

### precise

Runtime знает:

```text
вот это pointer
вот это int
```

а не рассматривает любое похожее число как возможный address.

### concurrent

Основная GC work выполняется одновременно с application goroutines.

### parallel

GC work могут выполнять несколько workers.

### non-generational

Нет привычного JVM-разделения:

```text
young
old
```

### non-compacting

GC обычно не перемещает живые heap objects только ради уплотнения памяти.

Следствие:

```text
pointer stability
```

проще, но fragmentation приходится решать allocator design, size classes и page management.

---

## 24. Но с Go 1.26 появился Green Tea

Вот здесь старые статьи по Go GC начинают устаревать.

Классическая модель tracing collector:

```text
нашли object A
↓
просканировали A
↓
нашли B
↓
просканировали B
↓
нашли C
```

Объекты могут лежать где угодно:

```text
A → heap page 17
B → heap page 900
C → heap page 41
D → heap page 3000
```

CPU получает:

```text
pointer chasing
+
cache miss
+
cache miss
+
cache miss
```

Green Tea меняет организацию mark work.

---

## 25. Главная идея Green Tea

Вместо стратегии:

```text
увидели объект
→ сразу сканируем
```

идея примерно такая:

```text
увидели объекты одного span
        ↓
накапливаем mark work
        ↓
сканируем их пачкой
```

Условно было:

```text
scan A on span 1
scan B on span 923
scan C on span 1
scan D on span 311
scan E on span 1
```

Хотим:

```text
span 1:
  scan A
  scan C
  scan E

span 311:
  scan D

span 923:
  scan B
```

CPU cache говорит спасибо.

---

## 26. marks и scans

Для Green Tea одной информации:

```text
object marked
```

недостаточно.

Нужно различать:

```text
объект обнаружен
```

и:

```text
объект уже просканирован
```

Поэтому алгоритм использует две концепции:

```text
marks
scans
```

В simplified форме:

```text
mark bit = объект reachable обнаружен
scan bit = pointers объекта уже просканированы
```

Когда впервые обнаруживается pointer на object:

```text
set mark
↓
queue span
```

Позже span обрабатывается пачкой.

---

## 27. Теперь GC тоже имеет локальные очереди P

Мы уже видели:

```text
P → mcache
```

в allocator.

В Green Tea появляется похожая идея локальности GC work:

```text
P
├── gcWork
│
├── work buffers
└── span queue
```

Span queue P-local, но work может быть опубликован и украден другими P.

То есть снова знакомый runtime design:

```text
локальность сначала
↓
sharing позже
```

Мы уже видели это в scheduler:

```text
local run queue
↓
global queue / steal
```

Allocator:

```text
mcache
↓
mcentral
```

GC:

```text
local gc work
↓
shared / stealing
```

Один архитектурный паттерн повторяется по всему runtime.

---

## 28. GC cycle

Теперь весь lifecycle.

Упрощённо:

```text
GC off / sweep
      │
      ▼
GC trigger
      │
      ▼
STW
      │
      ├── prepare marking
      ├── enable write barrier
      └── prepare roots
      │
      ▼
start world
      │
      ▼
concurrent mark
      │
      ├── GC workers
      ├── mutator assists
      ├── stack scanning
      └── write barriers
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

---

## 29. GC roots

Graph traversal откуда-то должен начаться.

Roots включают:

```text
goroutine stacks
globals
runtime structures containing heap pointers
```

Дальше:

```text
roots
 ↓
heap object
 ↓
heap object
 ↓
heap object
```

При concurrent marking runtime сканирует stack конкретной goroutine, временно останавливая её на время scan, после чего она продолжает выполнение.

Обратите внимание:

```text
stack scan
≠
остановить весь процесс на время scanning всех stacks
```

Это важная причина низких STW pauses.

---

## 30. Что происходит с объектами, созданными во время GC

Парадокс:

GC уже начался.

Программа продолжает работать.

Она создаёт:

```text
new objects
```

Что с ними делать?

Если считать их белыми:

```text
allocate
↓
GC ещё не знает объект
↓
можно ошибочно reclaim
```

Поэтому во время mark phase newly allocated heap objects считаются уже marked — условно сразу «black».

---

## 31. Write barrier: зачем он вообще нужен

Теперь классическая проблема concurrent GC.

Пусть:

```text
A → B
```

GC уже просканировал `A`.

Application делает:

```text
A → C
```

пока collector работает.

Heap graph изменился прямо во время graph traversal.

Получается гонка смыслов:

```text
GC видит одну версию graph
mutator создаёт другую
```

Если ничего не делать, reachable object можно потерять.

---

## 32. Hybrid write barrier

Go использует hybrid write barrier, сочетающий идеи Yuasa deletion barrier и Dijkstra insertion barrier.

Учебно можно представить:

```text
before:

slot → old

write:

slot → new
```

Barrier делает дополнительную GC bookkeeping работу вокруг pointer write, чтобы concurrent collector не потерял ни `old`, ни `new` object из tracing invariant.

Важно:

```text
pointer assignment
```

во время mark phase может стоить дороже обычного store.

---

## 33. Почему нельзя просто смотреть на цвет destination object

Наивная идея:

```text
если объект уже black
    barrier
иначе
    обычный store
```

Но тогда mutator и collector одновременно читают/пишут:

```text
pointer slot
mark state
```

и возникает memory-ordering проблема.

Чтобы условная проверка была гарантированно корректна на современном CPU, потребовались бы дополнительные synchronization/barrier costs.

И вот здесь наш блок:

```text
CAS
memory barriers
MESI
```

из предыдущей дополнительной лекции внезапно встречается с GC.

---

## 34. Write barrier работает не всегда

Barrier включается во время:

```text
_GCmark
_GCmarktermination
```

и выключается вне marking phase.

То есть цена concurrent correctness платится преимущественно тогда, когда GC действительно маркирует heap.

Компилятор также может опускать barriers для некоторых writes в текущий stack frame, поскольку stack имеет другие invariants.

---

## 35. Mark workers

Кто физически выполняет marking?

Не существует отдельного волшебного:

```text
GC thread
```

который делает всё.

Runtime использует несколько механизмов:

```text
dedicated mark workers
fractional mark workers
idle mark workers
mutator assists
```

---

## 36. Dedicated workers

GC pacer стремится выделять marking примерно:

```text
25% × GOMAXPROCS
```

CPU capacity.

Например очень грубо:

```text
GOMAXPROCS = 8

GC background target ≈ 2 CPUs
```

Но это не означает:

> GC всегда съедает строго 25% CPU.

Это pacing goal для background marking.

---

## 37. Idle workers

Допустим:

```text
GOMAXPROCS=8
```

а application реально использует только:

```text
2 CPU
```

Оставшиеся CPU idle.

Runtime может дать GC дополнительную работу.

То есть CPU profile иногда показывает:

```text
runtime.gcBgMarkWorker
```

с заметной долей CPU.

Это не обязательно означает:

> GC украл всё это CPU у requests.

Часть работы могла выполняться idle-priority workers на CPU, который application всё равно не использовала.

---

## 38. GC pacer

Теперь самое интересное.

У нас есть:

```text
allocation rate
```

и:

```text
mark throughput
```

GC должен закончить работу вовремя.

Нельзя просто сказать:

```text
heap достиг 2 GB
→ начинаем GC
```

если 2 GB — это уже максимальный target.

Тогда поздно.

---

## 39. Heap goal ≠ GC trigger

Это одна из самых важных вещей во всей дополнительной лекции.

Допустим:

```text
heap goal = 2 GB
```

Это означает примерно:

> Желательно закончить GC до достижения этого размера.

Следовательно GC должен стартовать раньше:

```text
trigger < heap goal
```

Насколько раньше?

Зависит от того:

```text
как быстро приложение аллоцирует
```

и:

```text
как быстро collector сканирует
```

Очень хорошая формулировка студентам:

```text
heap goal = финишная черта
GC trigger = точка старта
pacer = тот, кто рассчитывает, когда надо стартовать
```

---

## 40. Уточняем формулу GOGC

В основной лекции можно использовать упрощение:

```text
goal ≈ live + live × GOGC / 100
```

Но здесь уже пора дать более точную учебную формулу:

```text
Target heap =
Live heap
+
(Live heap + GC roots) × GOGC / 100
```

Roots учитываются в этой модели начиная с Go 1.18.

Например:

```text
live heap = 800 MB
scannable roots = 100 MB
GOGC = 100
```

Условный target:

```text
800
+
(800 + 100)
=
1700 MB
```

И снова:

**это goal, а не команда начать GC ровно на 1700 MB.**

---

## 41. Почему roots входят в pacing

Представим два приложения.

### Application A

```text
heap = 1 GB
100 goroutines
```

### Application B

```text
heap = 1 GB
500 000 goroutines
```

У B значительно больше stack roots.

GC должен их сканировать.

Если pacer учитывал бы только heap:

```text
A и B
```

выглядели бы одинаковыми.

Но workload GC разный.

Поэтому root scan work тоже входит в pacing model.

---

## 42. Mutator assists

Теперь pacer ошибся.

Или allocation rate неожиданно вырос.

Например:

```text
GC успевает scan:
1 GB/s

application внезапно allocates:
10 GB/s
```

Если дать application продолжать без ограничения:

```text
heap goal
```

будет пробит раньше, чем collector закончит.

Поэтому allocating goroutines получают GC debt.

Условно:

```text
ты выделил N bytes
↓
ты создал дополнительную GC work
↓
помоги выполнить часть mark work
```

Это mutator assist.

---

## 43. Assist ratio

Runtime рассчитывает примерно:

```text
сколько scan work
должно соответствовать
каждому allocated byte
```

В runtime это отражено через:

```text
assistWorkPerByte
```

и обратное отношение.

Получаем:

```text
allocation
↓
GC debt
↓
goroutine выполняет marking
↓
debt погашен
↓
goroutine продолжает user work
```

---

## 44. Почему GC pressure превращается в latency

Вот production chain:

```text
HTTP request
↓
JSON parsing
↓
DTO
↓
temporary slices
↓
allocations
↓
GC debt
↓
mark assist
↓
request goroutine выполняет GC
↓
handler выполняется дольше
↓
p99 растёт
```

Никакого большого STW.

Никакой секундной GC pause.

Но latency ухудшается.

Поэтому утверждение:

> «GC pause всего 200 µs — GC точно ни при чём»

неверно.

---

## 45. Как увидеть assists

В современном `runtime/metrics` есть:

```text
/cpu/classes/gc/mark/assist:cpu-seconds
/cpu/classes/gc/mark/dedicated:cpu-seconds
/cpu/classes/gc/mark/idle:cpu-seconds
/cpu/classes/gc/total:cpu-seconds
```

То есть можно отдельно увидеть:

```text
сколько CPU ушло в assists
```

и:

```text
сколько отработали background workers
```

Это уже намного полезнее фразы:

```text
GC CPU = 20%
```

---

## 46. Mark termination

Когда runtime считает, что work queues опустели:

```text
roots processed
+
grey work processed
```

нужно убедиться:

```text
новой mark work действительно больше нет
```

Поскольку work распределён между локальными GC structures разных P, используется distributed termination detection.

После этого:

```text
STW
↓
mark termination
```

Workers и assists выключаются, runtime выполняет финальную bookkeeping работу.

---

## 47. Sweep

Mark ответил:

```text
кто жив?
```

Но memory ещё надо сделать reusable.

Допустим:

```text
span slots:

A alive
B dead
C alive
D dead
```

После marking:

```text
mark bits:

1 0 1 0
```

Sweep превращает это в allocator state следующего поколения:

```text
allocated/free:

1 0 1 0
```

То есть B и D теперь можно снова выдавать allocator.

Runtime переиспользует GC mark bitmap как будущий allocation bitmap и создаёт свежие mark bits для следующего GC cycle.

Красивое переиспользование metadata.

---

## 48. Sweep тоже concurrent

Не обязательно:

```text
mark done
↓
STOP WORLD
↓
sweep entire heap
↓
start
```

Sweeping выполняется:

```text
background
```

и:

```text
в ответ на allocation
```

Runtime может sweep'ить span тогда, когда allocator хочет его использовать.

Отсюда ещё одна форма amortization:

```text
часть cleanup cost
распределяется по дальнейшим allocations
```

---

## 49. sweepgen

Как runtime понимает:

> Этот span уже sweep'нули после текущего GC или ещё нет?

Для этого существует `sweepgen`.

Упрощённо:

```text
heap sweep generation
```

увеличивается каждый GC cycle.

Span хранит свою generation.

По разнице runtime понимает состояния:

```text
needs sweep
being swept
already swept
cached
```

Студентам число конкретной прибавки помнить не надо.

Нужно понять сам механизм:

**span lifecycle не требует обходить весь heap каждый раз только ради вопроса «этот span уже обработан?»**

---

## 50. Что происходит с полностью пустым span

Если после sweep:

```text
span:
0 live objects
```

его object slots больше не нужны.

Страницы span возвращаются:

```text
mspan
↓
mheap
```

То есть allocator получает свободные pages.

Но внимание.

Это ещё не обязательно:

```text
memory returned to OS
```

Вот здесь появляется один из самых важных терминов лекции.

---

## 51. Sweep ≠ scavenging

Очень частое заблуждение:

> GC освободил память → Linux сразу получил RAM назад.

Нет.

Есть минимум два этапа.

### Sweep

```text
dead Go objects
↓
free allocator slots/pages
```

Memory снова доступна **Go runtime**.

### Scavenger

```text
free runtime pages
↓
OS informed that physical memory may be reclaimed
```

То есть:

```text
GC reclaim
≠
OS reclaim
```

---

## 52. Зачем вообще оставлять память у runtime

Представим сервис:

```text
09:00 peak
heap = 4 GB

09:05
heap live = 1 GB

09:06
новый traffic peak
heap снова нужно 4 GB
```

Если runtime немедленно всё отдаст OS:

```text
release pages
↓
через минуту снова ask OS
↓
page faults / kernel work
```

Если runtime удержит всё:

```text
быстрый reuse
```

но:

```text
RSS высокий
```

Получаем очередной trade-off:

```text
reuse speed
↔
memory footprint
```

Именно им занимается scavenger.

---

## 53. Background scavenger

В runtime существует отдельная system goroutine scavenger.

Она ищет свободные heap pages, которые можно вернуть underlying platform.

Получаем:

```text
GC
↓
dead objects

sweep
↓
free Go pages

scavenger
↓
released physical pages
```

---

## 54. Как это выглядит на Linux

Runtime вызывает abstraction:

```text
sysUnused
```

На Linux основной mechanism — `madvise`.

Текущая реализация использует механизмы вроде:

```text
MADV_FREE
```

и:

```text
MADV_DONTNEED
```

в зависимости от платформенных условий.

Это уже настоящий переход:

```text
Go runtime
↓
kernel VM subsystem
```

---

## 55. Runtime page ≠ physical page

Runtime page:

```text
8 KiB
```

Но OS physical page может иметь другой размер.

Например на разных архитектурах:

```text
4 KiB
16 KiB
...
```

Scavenger может освобождать только целые physical pages, поэтому runtime учитывает размер страницы ОС и выравнивает release operations соответствующим образом.

Это особенно хороший пример того, где language/runtime abstraction начинает протекать в OS.

---

## 56. OS memory states

У runtime есть собственная abstraction над virtual memory.

Регион может быть концептуально:

```text
None
Reserved
Prepared
Ready
```

`Ready`:

```text
memory безопасно доступна runtime
```

`Prepared`:

```text
address space остаётся,
но physical backing runtime сейчас не требует
```

`Reserved`:

```text
address range принадлежит runtime,
но memory нельзя использовать как обычную
```

Такая модель позволяет runtime резервировать virtual address space отдельно от фактического physical memory usage.

---

## 57. Почему heap упал, а RSS не упал

Представим Grafana:

```text
HeapAlloc:

4 GB
↓
1 GB
```

а:

```text
RSS:

4.6 GB
↓
4.2 GB
```

Инженер говорит:

> GC не освободил память.

Возможно освободил.

Но разные метрики отвечают на разные вопросы.

```text
live objects
```

может резко уменьшиться.

Allocator может получить:

```text
free spans/pages
```

Scavenger ещё не вернул их OS.

Или OS получил `MADV_FREE`, но физически ещё не reclaimed страницы.

Поэтому:

```text
heap live
heap free
heap released
RSS
```

нельзя считать синонимами.

---

## 58. runtime/metrics для allocator

Для нормальной диагностики полезны:

```text
/memory/classes/heap/objects:bytes
/memory/classes/heap/free:bytes
/memory/classes/heap/released:bytes
/memory/classes/heap/unused:bytes
```

Отдельно runtime exposes metadata:

```text
/memory/classes/metadata/mcache/inuse:bytes
/memory/classes/metadata/mspan/inuse:bytes
```

То есть модель:

```text
heap objects
+
heap free
+
heap released
+
allocator metadata
+
stacks
+
other runtime memory
```

намного ближе к реальной памяти процесса, чем одно число `HeapAlloc`.

---

## 59. GOMEMLIMIT входит в ту же систему

Memory limit влияет не только на:

```text
когда запускать GC
```

Он влияет на общую runtime memory-management policy.

Runtime считает controlled memory примерно как:

```text
runtime mapped memory
-
released heap memory
```

В терминах metrics:

```text
/memory/classes/total:bytes
-
/memory/classes/heap/released:bytes
```

То есть ограничение касается больше, чем одного live heap.

---

## 60. Что делает pacer при memory pressure

Допустим:

```text
container limit = 1 GiB
GOMEMLIMIT = 900 MiB
```

Runtime видит:

```text
memory usage approaching limit
```

и уменьшает допустимый heap goal.

Следствие:

```text
GC starts earlier
↓
cycles become more frequent
↓
scavenger becomes more relevant
```

Если live set сам уже близок к memory limit:

```text
live = 850 MB
limit = 900 MB
```

runtime почти не получает пространства:

```text
для новых allocations
+
для завершения GC
```

Начинается тяжёлый режим.

---

## 61. GC thrashing

Цепочка:

```text
limit too low
↓
heap goal tiny
↓
GC frequently triggered
↓
GC CPU rises
↓
allocating goroutines assist
↓
application CPU falls
↓
latency rises
↓
GC finishes
↓
почти сразу следующий GC
```

Это GC thrashing.

`GOMEMLIMIT` soft именно потому, что runtime должен сохранять progress даже при плохой конфигурации.

Это критический production-вывод:

**soft memory limit иногда нарушается специально, потому что бесконечный GC хуже кратковременного превышения лимита.**

---

## 62. А Kubernetes всё равно может убить pod

Go говорит:

```text
GOMEMLIMIT is soft
```

Kernel/cgroup говорит:

```text
memory limit is not philosophical
```

Кроме того, GOMEMLIMIT не контролирует абсолютно всю память процесса:

- некоторые mmap;
- cgo/native allocations;
- kernel-side memory;
- прочие external sources.

Поэтому:

```text
container limit = 1 GiB
GOMEMLIMIT = 1 GiB
```

— плохая идея.

Нужен headroom.

---

## 63. Полная связь allocator и GC

Теперь соберём обе подсистемы.

```text
APPLICATION
    │
    │ allocation
    ▼
escape analysis
    │
    ▼
allocator
    │
    ├── mcache
    ├── mspan
    ├── mcentral
    └── mheap
    │
    ▼
heap grows
    │
    ▼
GC pacer
    │
    ▼
GC trigger
    │
    ▼
mark
    │
    ├── roots
    ├── Green Tea span work
    ├── workers
    ├── write barrier
    └── assists
    │
    ▼
sweep
    │
    ▼
free slots / free pages
    │
    ├── reused by allocator
    │
    └── scavenger
           │
           ▼
          OS
```

Это одна система.

Нельзя отдельно понимать:

```text
allocator
```

и отдельно:

```text
GC
```

Они постоянно обмениваются state.

---

## 64. Что происходит под нагрузкой

Теперь типичный Go backend.

Normal load:

```text
5k RPS
allocation rate = 500 MB/s
GC CPU = 5%
```

При traffic spike:

```text
25k RPS
allocation rate = 3 GB/s
```

Сначала:

```text
mcache fast allocations
```

работают отлично.

Но дальше:

```text
heap grows faster
↓
GC cycles become more frequent
↓
background mark CPU grows
↓
assist ratio grows
↓
request goroutines perform GC work
↓
CPU contention
↓
p99 increases
```

Allocator сам по себе мог оставаться быстрым.

Цена проявилась позже в collector.

---

## 65. «Allocation дешёвая» — опасное упрощение

Да.

Fast-path allocation может быть очень дешёвой:

```text
local P
+
mcache
+
bitmap
```

Но lifetime cost allocation:

```text
allocation
+
zeroing
+
heap growth
+
future marking
+
future sweeping
+
possible scavenging
+
cache pressure
```

Поэтому правильная модель:

> Heap allocation может быть дешёвой сейчас и дорогой позже.

---

## 66. Почему object pooling тоже не бесплатный подарок

После этой лекции студенты могут сделать вывод:

```text
allocations bad
↓
sync.Pool everything
```

Тоже ошибка.

Pooling:

```text
↓ allocation rate
```

но может:

```text
↑ retained memory
↑ object lifetime
↑ complexity
↑ stale-data risks
```

А очень долгоживущие pointer-rich objects всё равно участвуют в scan work.

Поэтому:

```text
measure
↓
find hotspot
↓
optimize
```

а не:

```text
pool everything
```

---

## 67. Java-мост: allocator

Java:

```text
Thread
↓
TLAB
↓
Eden
```

Go:

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
```

Главное различие:

Go allocator тесно встроен в scheduler через P.

Goroutine не владеет allocator cache.

---

## 68. Java-мост: GC

Современный JVM разработчик привык видеть:

```text
young generation
old generation
regions
evacuation
compaction
```

Go GC другая архитектура:

```text
non-generational
non-compacting
concurrent mark-and-sweep
```

А Green Tea оптимизирует прежде всего locality самого tracing/marking, вместо перехода к классической generational moving architecture.

Поэтому нельзя переводить напрямую:

```text
Go mspan = G1 region
```

Модели разные.

---

## 69. Production-расследование №1: CPU

Симптом:

```text
CPU 95%
RSS 800 MB
heap live 300 MB
p99 900 ms
```

CPU pprof:

```text
runtime.gcBgMarkWorker
runtime.scanobject
runtime.mallocgc
```

Metrics:

```text
mark assist CPU ↑
alloc rate ↑
GC cycles/sec ↑
```

Что произошло?

Не обязательно:

```text
слишком большой heap
```

а возможно:

```text
огромная churn rate
```

Например:

```text
JSON
↓
temporary DTO
↓
strings
↓
[]byte conversions
↓
millions of short-lived objects
```

Live heap маленький.

GC work огромный.

---

## 70. Production-расследование №2: RSS

Симптом:

```text
traffic spike закончился

heap live:
3 GB → 700 MB

RSS:
3.8 GB → 3.4 GB
```

Плохой вывод:

> memory leak.

Правильное расследование:

```text
heap objects?
heap free?
heap released?
RSS?
```

Если:

```text
heap free high
```

значит allocator уже имеет свободные pages.

Если позже:

```text
heap released ↑
```

значит scavenger начал возвращать память platform.

Это может быть normal allocator/scavenger behaviour.

---

## 71. Production-расследование №3: GOMEMLIMIT

Симптом:

```text
pod memory limit = 512 MiB
GOMEMLIMIT = 450 MiB
live heap = 410 MiB
```

Под нагрузкой:

```text
CPU ↑
latency ↑
memory почти не растёт
```

Heap profile leak не показывает.

Смотрим:

```text
GC cycles ↑
mark assist CPU ↑
GC limiter activity
```

Причина:

```text
слишком мало GC runway
```

Runtime пытается жить внутри слишком тесного memory budget.

Проблема памяти стала проблемой CPU.

---

## 72. Что смотреть руками

Для дополнительной лекции я бы обязательно показал `runtime/metrics`:

```text
/gc/heap/live:bytes
/gc/heap/goal:bytes
/gc/heap/allocs:bytes
/gc/cycles/total:gc-cycles

/gc/scan/heap:bytes
/gc/scan/stack:bytes

/cpu/classes/gc/mark/assist:cpu-seconds
/cpu/classes/gc/mark/dedicated:cpu-seconds
/cpu/classes/gc/mark/idle:cpu-seconds

/memory/classes/heap/free:bytes
/memory/classes/heap/released:bytes

/cpu/classes/scavenge/assist:cpu-seconds
/cpu/classes/scavenge/background:cpu-seconds
```

---

## 73. Практический эксперимент: увидеть allocator

Сделать benchmark:

```go
type Small struct {
    A int64
    B int64
}

func BenchmarkAlloc(b *testing.B) {
    for b.Loop() {
        x := new(Small)
        sink = x
    }
}
```

Запустить:

```bash
go test -bench=. -benchmem
```

Дальше изменить размер структуры:

```text
16 B
24 B
32 B
40 B
48 B
...
```

И посмотреть:

```text
B/op
allocs/op
```

Обсудить:

```text
requested object size
↓
size class
↓
real allocator footprint
```

---

## 74. Практический эксперимент: scan vs noscan

Сравнить:

```go
type NoPointers struct {
    A uint64
    B uint64
    C uint64
    D uint64
}
```

и:

```go
type WithPointers struct {
    A *int
    B *int
    C *int
    D *int
}
```

Создать большой live heap обоих вариантов.

Сравнить:

```text
heap size
GC CPU
/gc/scan/heap:bytes
```

Главный тезис:

```text
same bytes
≠
same GC cost
```

---

## 75. Практический эксперимент: allocation pressure

Создать HTTP handler:

```go
func handler(w http.ResponseWriter, r *http.Request) {
    for range 1000 {
        _ = make([]byte, 1024)
    }
}
```

Специально добиться escape.

Нагрузить.

Смотреть:

```text
alloc rate
GC cycles
mark assists
CPU
latency
```

Потом убрать лишние allocations.

Повторить.

---

## 76. Практический эксперимент: GOMEMLIMIT

Запустить один и тот же workload:

```text
GOMEMLIMIT=2GiB
GOMEMLIMIT=1GiB
GOMEMLIMIT=500MiB
```

И сравнить:

```text
heap goal
GC cycles/sec
GC CPU
assist CPU
latency
```

Студент должен увидеть руками:

```text
memory budget ↓
→
GC frequency ↑
→
CPU cost ↑
```

---

## 77. Практический эксперимент: scavenger

Сценарий:

```text
allocate several GB
↓
drop references
↓
force GC / wait
```

Снимать:

```text
heap objects
heap free
heap released
RSS
```

И увидеть четыре разные линии.

Это лучший способ убить навсегда заблуждение:

```text
GC = вернуть RAM Linux
```

---

## 78. Вопросы студентам

1. Почему `mcache` принадлежит P, а не goroutine?

2. Почему runtime выдаёт `mcache` целый span, а не один free object из `mcentral`?

3. Почему два объекта одинакового размера могут попасть в разные span classes?

4. Почему heap из `[]byte` потенциально дешевле для GC, чем heap того же размера из `[]*Node`?

5. Что произойдёт, если allocation rate внезапно станет выше scan throughput?

6. Почему маленькая STW pause не доказывает, что GC не влияет на p99?

7. В чём разница между sweep и scavenging?

8. Почему после успешного GC RSS может практически не уменьшиться?

9. Почему слишком низкий `GOMEMLIMIT` может увеличить CPU usage?

10. Почему collector стартует раньше heap goal?

11. Что Green Tea пытается исправить на уровне CPU cache?

12. Почему обычный graph traversal heap может быть microarchitecturally дорогим?

13. Почему Go использует write barrier во время concurrent marking?

14. Почему проверка «destination object уже black?» сама по себе может потребовать дорогой memory ordering?

15. Почему `sync.Pool` не является универсальным лечением allocation pressure?

---

## 79. Что важно запомнить

1. **Fast path маленькой allocation идёт через per-P `mcache` и `mspan`; `mcentral`, `mheap` и OS — более редкие slow paths.**

2. **`mspan` связывает allocator и GC: allocator использует allocation bits, collector — mark state.**

3. **Size classes уменьшают allocator complexity ценой internal fragmentation.**

4. **`spanClass` учитывает не только размер, но и `scan/noscan`; наличие pointers влияет на стоимость GC.**

5. **Large allocations идут page-level path, а tiny pointer-free allocations могут упаковываться вместе.**

6. **Go GC — concurrent, precise, non-generational, non-compacting mark-and-sweep collector.**

7. **Начиная с Go 1.26 default GC — Green Tea; его ключевая идея — группировать marking/scanning work ради лучшей memory locality.**

8. **Heap goal — финиш GC cycle, trigger — точка старта. Pacer пытается подобрать trigger по allocation и scan rates.**

9. **Если GC отстаёт, allocating goroutines сами выполняют marking через mutator assists. Это напрямую может увеличивать request latency.**

10. **Sweep освобождает память для Go allocator. Scavenger возвращает свободные physical pages underlying OS. Это разные процессы.**

11. **Heap, free heap, released heap и RSS — разные метрики.**

12. **Memory pressure часто проявляется как CPU pressure: tighter memory budget → more GC → more assists → less CPU for backend.**

---

# Главная инженерная цепочка лекции

```text
escape
↓
heap allocation
↓
mcache
↓
mspan
↓
mcentral
↓
mheap
↓
heap growth
↓
GC pacer
↓
concurrent mark
↓
Green Tea
↓
write barriers
↓
mark workers + assists
↓
sweep
↓
free pages
↓
scavenger
↓
OS
```

И финальная мысль:

**Go memory runtime — это не «GC иногда очищает heap». Это непрерывно работающая система allocator + collector + scheduler + OS memory manager. Любая интенсивная allocation проходит через эту систему сейчас, а её реальную цену backend может заплатить позже CPU, latency или RSS.**
