# Доп. 1. Глубокие internals Go runtime, synchronization и CPU

> Дополнительный блок к теме **«Go runtime, concurrency и synchronization internals»**.
>
> Эта версия расширена до уровня технического расследования: для каждого слоя рассматриваются не только названия структур и функций, но и причины дизайна, state transitions, contention, стоимость под нагрузкой и связь с диагностикой backend.
>
> Здесь мы спускаемся ниже обычного прикладного Go: от scheduler lifecycle и `sudog`
> до `sync.Mutex.state`, CAS, cache coherence, false sharing и NUMA.
>
> Цель — не научиться переписывать `runtime`, а понимать, **почему backend под нагрузкой ведёт себя именно так**.

---

# 0. Перед началом: где гарантия языка, а где implementation detail

В этой теме особенно легко смешать три уровня.

## Публичный контракт Go

Это то, на что прикладная программа имеет право рассчитывать:

- семантика `go`;
- семантика channels;
- гарантии `sync.Mutex`;
- гарантии `sync/atomic`;
- Go Memory Model;
- поведение публичных API.

Например, `Unlock` одного `Mutex` synchronizes before последующего успешного `Lock` этого же mutex. Это часть публичной модели синхронизации.

## Детали текущей реализации runtime

Сюда относятся:

- `G`, `M`, `P`;
- `newproc`;
- `runqput`;
- `runnext`;
- `findRunnable`;
- `runqsteal`;
- `sudog`;
- `semaRoot`;
- конкретные биты `Mutex.state`;
- starvation threshold около 1 ms;
- конкретное число попыток stealing.

Это **не контракт языка**. В будущей версии Go внутренний алгоритм может измениться при сохранении публичной семантики.

## Учебная модель

Иногда мы специально говорим проще:

```text
G блокируется
↓
scheduler запускает другую G
```

Реальная цепочка может включать `gopark`, `mcall`, `park_m`, `g0`, `dropg`, runtime locks, trace hooks и другие детали.

На лекции важно понимать причинную цепочку, а не помнить каждую строку `proc.go`.

---

# 1. Детальный lifecycle scheduler и внутренние функции runtime

## 1.1. От `go f()` до runnable goroutine

Пишем:

```go
go worker()
```

На уровне языка это означает запуск `worker` в новой goroutine.

В runtime путь концептуально выглядит так:

```text
go worker()
↓
runtime.newproc
↓
runtime.newproc1
↓
получить/создать G
↓
подготовить stack и execution context
↓
_Gdead → _Grunnable
↓
runqput
↓
возможно wakep
```

Важно: `go` не создаёт OS thread.

Он создаёт новую единицу работы scheduler — `G`.

---

## 1.2. `newproc`

Compiler превращает `go worker()` в вызов runtime machinery.

`newproc` должен создать execution context новой goroutine:

```text
что выполнять?
↓
с каким stack pointer?
↓
с каким стартовым PC?
↓
в каком scheduler state?
```

Runtime не обязан каждый раз выделять новую структуру `g` с нуля.

Завершившиеся G переиспользуются через free lists.

Получается:

```text
goroutine завершилась
↓
G → _Gdead
↓
G попадает в runtime pool
↓
следующий go statement
↓
структура G может быть переиспользована
```

Это одна из причин, почему создание goroutine дешевле создания полноценного OS thread.

Но stack и runtime metadata всё равно имеют цену.

---

## 1.3. `newproc1`

Упрощённая задача `newproc1`:

1. получить свободную `G`;
2. подготовить её stack;
3. записать стартовый execution context;
4. назначить goroutine id;
5. перевести G в `_Grunnable`;
6. передать её scheduler.

Ментальная модель:

```text
G существует
но ещё нигде не выполняется

_Gdead
↓
setup
↓
_Grunnable
```

С этого момента scheduler имеет право выбрать её для исполнения.

---

# 2. `runqput`: куда кладётся новая G

У каждого `P` есть local run queue.

Упрощённо:

```text
P0
├── runnext
└── runq:
    G12
    G35
    G41
```

Когда новая G становится runnable, runtime старается использовать локальную очередь текущего P.

Почему не одна глобальная очередь?

Если все P делают:

```text
P0 ─┐
P1 ─┤
P2 ─┼──► GLOBAL QUEUE
P3 ─┤
... │
PN ─┘
```

global queue становится общей точкой coordination.

Local queues дают:

```text
меньше shared synchronization
+
лучше cache locality
+
лучше масштабирование scheduler
```

---

## 2.1. Что если local queue переполнена

Local queue конечна.

Когда она переполняется, runtime должен часть работы перенести наружу.

Упрощённая модель:

```text
P0 local queue FULL
↓
часть G
+
новая G
↓
global run queue
```

Это возвращает баланс:

- local queue оптимизирует fast path;
- global queue служит общей точкой redistribution/fairness.

Точные размеры очередей и batch-алгоритм — implementation detail.

---

# 3. `schedule()`

Когда текущая G перестаёт выполняться, M должен найти следующую работу.

Типовые причины:

```text
G закончилась
G заблокировалась
G сделала Gosched
G была preempted
```

Runtime переходит к scheduler:

```text
schedule()
↓
найти runnable work
↓
execute(nextG)
```

`runtime.schedule()` — верхний цикл выбора следующей работы.

---

# 4. `findRunnable()`

Вот здесь начинается настоящее планирование.

Неверная модель:

> Scheduler берёт следующую G из очереди.

Очередь не одна.

У scheduler есть несколько источников work:

```text
local run queue
global run queue
netpoller
timers
GC/runtime work
work stealing
```

Упрощённо:

```text
findRunnable
↓
runtime/GC work?
↓
local runq?
↓
global runq?
↓
non-blocking netpoll?
↓
steal from another P?
↓
timers / повторные проверки?
↓
blocking netpoll / park?
```

Точный порядок — implementation detail текущего runtime.

Смысл алгоритма важнее:

> Сначала ищем дешёвую доступную работу, затем расширяем область поиска, а если работы действительно нет — перестаём жечь CPU.

---

# 5. `runqget()`

Сначала scheduler пытается получить work с текущего P.

У current P есть:

```text
runnext
+
ordinary local runq
```

В текущей реализации `runqget` сначала проверяет `runnext`.

Если `runnext` пуст:

```text
runq head
↓
следующая G
```

Это очень дешёвый путь: P работает со своей собственной очередью.

---

# 6. `execute()`

Допустим scheduler нашёл:

```text
G42 = RUNNABLE
```

`execute` делает ключевую связь:

```text
M.curg = G42
G42.m = M
```

после чего:

```text
_GRunnable
↓
_GRunning
```

и execution context G восстанавливается.

Концептуально:

```text
scheduler stack / g0
↓
execute(G42)
↓
G42 user stack
↓
продолжение Go-кода
```

Именно поэтому G — не просто функция.

Runtime должен сохранить достаточно context, чтобы goroutine можно было:

```text
остановить
↓
положить в очередь
↓
через некоторое время
↓
продолжить с того же места
```

---

# 7. Когда G блокируется: `gopark → park_m`

Например:

```go
x := <-ch
```

а данных нет.

G продолжить выполнение не может.

Путь:

```text
G RUNNING
↓
register waiter
↓
gopark
↓
mcall(park_m)
↓
switch to M.g0
↓
RUNNING → WAITING
↓
dropg
↓
schedule
```

Важно разделять:

```text
G перестала выполняться
```

и:

```text
OS thread перестал выполняться
```

При `gopark` обычно первое происходит без второго.

M возвращается scheduler и может выполнять другую G.

---

# 8. `g0`: зачем scheduler отдельный stack

Возникает проблема.

Мы собираемся перестать выполнять G1:

```text
G1 stack
↓
gopark
```

Но scheduler должен:

- изменить state G1;
- отвязать её от M;
- выбрать другую G;
- переключить execution context.

Делать это на stack G1 неудобно и в некоторых местах невозможно.

У каждого M есть специальная system goroutine:

```text
g0
```

и system stack.

Упрощённо:

```text
M
├── curg → user G
└── g0   → runtime scheduler stack
```

При scheduler/runtime transitions:

```text
user G
↓
mcall/systemstack
↓
g0
↓
runtime machinery
```

`g0` — фундаментальная часть runtime internals.

---

# 9. `dropg()`

Пока G выполняется:

```text
M.curg → G
G.m    → M
```

После парковки эта association больше не нужна.

`dropg()` концептуально делает:

```text
M.curg = nil
G.m = nil
```

Теперь:

```text
G = WAITING
M = свободен для scheduler
```

M вместе с P может выполнить другую runnable goroutine.

---

# 10. Пробуждение: `goready → ready`

Позже событие произошло:

- sender прислал значение;
- mutex освободился;
- timer сработал;
- network socket стал ready.

Waiting G можно вернуть scheduler:

```text
WAITING
↓
ready / goready
↓
RUNNABLE
↓
run queue
```

Очень важно:

```text
goready
≠
"немедленно продолжить G"
```

`goready` означает:

> Эта G снова имеет право выполняться.

Но между `RUNNABLE` и `RUNNING` может быть scheduler delay.

---

# 11. Завершение G

Когда goroutine возвращается из top-level function, runtime проходит через `goexit`.

Концептуально:

```text
G RUNNING
↓
function returned
↓
goexit / goexit1
↓
gdestroy
↓
_Gdead
↓
G reusable
↓
schedule next G
```

G не обязана сразу освобождаться как объект памяти.

Runtime переиспользует G structures.

---


## 11.1. Scheduler lifecycle лучше читать как state machine

Если смотреть на scheduler только как на набор функций `newproc`, `schedule`,
`findRunnable`, `execute`, легко потерять главное: runtime постоянно переводит G
между небольшим числом состояний.

Упрощённая карта:

```text
                   newproc
                     │
                     ▼
                 RUNNABLE
                     │
                  execute
                     │
                     ▼
                  RUNNING
                /    |     \
               /     |      \
          gopark   preempt   goexit
             │        │        │
             ▼        ▼        ▼
          WAITING  RUNNABLE   DEAD
             │
           ready
             │
             └──────────────► RUNNABLE
```

Эта схема полезнее запоминания исходников, потому что объясняет практически любой
runtime-сценарий.

Например, channel receive без sender:

```text
RUNNING
↓
WAITING
```

Срабатывание timer:

```text
WAITING
↓
RUNNABLE
```

Async preemption:

```text
RUNNING
↓
RUNNABLE
```

Завершение функции goroutine:

```text
RUNNING
↓
DEAD
```

Разница между `WAITING` и `RUNNABLE` принципиальна. В первом случае G физически
не может продолжить работу до внешнего события. Во втором она готова работать,
но пока не получила execution resource.

---

## 11.2. Что именно сохраняется при переключении G

Scheduler не может просто помнить:

```text
"потом снова вызвать эту функцию"
```

Потому что goroutine может находиться глубоко внутри call chain:

```text
handler
↓
service
↓
repository
↓
decoder
↓
channel receive
```

После wakeup нужно продолжить именно после blocking operation, сохранив:

- call stack;
- локальные переменные;
- stack pointer;
- instruction pointer / continuation;
- runtime bookkeeping.

Поэтому `G` — это descriptor продолжения вычисления.

Ментальная модель:

```text
G
├── stack
├── saved execution context
├── status
├── wait metadata
└── scheduler metadata
```

Это объясняет, почему parking goroutine принципиально отличается от callback
модели: application code выглядит как обычный последовательный код, а runtime
умеет временно остановить и позже восстановить его execution context.

---

## 11.3. Почему scheduler работает через `g0`

Пусть G1 решила park'нуться.

Если runtime продолжит выполнять scheduler code на stack G1, возникает
неприятная зависимость: мы пытаемся изменять состояние и potentially перестать
использовать execution context, на котором сами сейчас стоим.

Поэтому у каждого M есть special runtime context:

```text
M
├── curg → текущая прикладная G
└── g0   → system goroutine / system stack
```

Переход:

```text
G1 user stack
↓
mcall / system transition
↓
M.g0 stack
↓
park_m / schedule / runtime work
```

После выбора G2:

```text
M.g0
↓
execute(G2)
↓
G2 user stack
```

`g0` не является обычной пользовательской goroutine. Она не участвует в
прикладном scheduling lifecycle как обычная G и служит runtime для операций,
которые должны выполняться вне movable user stack.

---

## 11.4. Почему `RUNNABLE` может быть дорогим состоянием

Waiting G почти не требует CPU:

```text
WAITING
↓
нет события
↓
scheduler её не рассматривает как работу
```

Runnable G — полноценный backlog CPU work:

```text
RUNNABLE
↓
должна получить P/M
```

Если сервис принимает CPU-bound работу быстрее, чем способен выполнять:

```text
arrival rate > CPU service rate
↓
RUNNABLE queue grows
↓
scheduler latency grows
↓
request latency grows
```

В production это важное различие.

Две системы могут иметь:

```text
50 000 goroutines
```

но совершенно разное состояние.

Система A:

```text
49 900 WAITING on network
100 RUNNING/RUNNABLE
```

может быть нормальной.

Система B:

```text
49 000 RUNNABLE
```

уже имеет огромный CPU backlog.

Поэтому число goroutines без их states почти бессмысленно.

---

## 11.5. Где появляется preemption в lifecycle

Preemption не создаёт отдельное состояние вида:

```text
PREEMPTED
```

В практической модели long-running G переводится обратно в runnable work:

```text
G RUNNING
↓
preemption requested
↓
safe preemption point
↓
G RUNNABLE
↓
scheduler может выбрать другую G
```

Смысл:

> G всё ещё способна продолжить вычисление, но runtime временно отобрал у неё
> execution resource ради fairness и progress системы.

Это отличается от parking:

```text
preemption:
RUNNING → RUNNABLE

parking:
RUNNING → WAITING
```

У этих переходов разные причины и разные последствия.

## Полный scheduler lifecycle

```text
go f()
↓
newproc
↓
newproc1
↓
G = RUNNABLE
↓
runqput
↓
schedule
↓
findRunnable
↓
runqget / global / netpoll / steal
↓
execute
↓
G = RUNNING
↓
─────────────────────────────
│                           │
│ block                     │ finish
▼                           ▼
gopark                    goexit
↓                           ↓
park_m                    gdestroy
↓                           ↓
WAITING                   DEAD
↓                           ↓
event                       pool
↓
goready
↓
RUNNABLE
↓
scheduler
```


## 11.6. Почему внутренние функции не являются API

Нельзя строить прикладную корректность на предположениях вида:

```text
newproc всегда кладёт новую G именно сюда
findRunnable всегда проверяет источники строго в таком порядке
runqsteal всегда крадёт ровно столько-то элементов
```

Это runtime implementation policy.

Публичная программа имеет право рассчитывать на семантику языка и
synchronization primitives, но scheduler internals могут меняться между
версиями Go.

Для преподавания полезно разделять:

```text
стабильная модель:
goroutine становится runnable → scheduler когда-нибудь её выполняет

деталь реализации:
какая queue, какой batch, какой конкретный helper function
```

Именно это позволяет изучать internals без превращения implementation detail в
ложный контракт языка.

### Что важно запомнить

- Scheduler оперирует G, а не пользовательскими функциями напрямую.
- `RUNNABLE` ещё не означает `RUNNING`.
- Blocking G обычно возвращает M/P scheduler.
- `gopark` и `goready` образуют фундаментальный wait/wakeup lifecycle.
- Названия внутренних функций — implementation detail; сама модель scheduling/parking — ключ к пониманию backend-поведения.

---

# 12. `runnext`

Теперь открываем следующий слой scheduler.

У P есть ordinary run queue и отдельный slot `runnext`.

```text
P0

runnext → G17

runq:
G42
G43
G44
```

Если `runnext` заполнен, scheduler даёт этой G шанс выполниться следующей.

---

# 13. Зачем нужен `runnext`

Представим:

```text
G1 выполняется
↓
G1 создаёт G2
```

Можно отправить G2 в конец local queue:

```text
G10
G11
G12
...
G2
```

Но G1 и G2 могут быть causally related.

Если G2 продолжает работу G1, немедленный запуск уменьшает latency и может лучше сохранить cache locality.

Поэтому scheduler иногда предпочитает:

```text
G1
↓
G2 = runnext
↓
G2 идёт следующей
```

---

# 14. `inheritTime`

Когда `runqget()` забирает G из `runnext`, он может вернуть:

```text
inheritTime = true
```

Это означает:

> Новая G наследует оставшуюся часть текущего scheduler time slice.

В текущей реализации `execute` при `inheritTime == false` увеличивает `P.schedtick`, а при `inheritTime == true` новый tick не начинается.

Получается:

```text
schedtick = 100

G1
↓
G2 via runnext
↓
G3 via runnext

всё ещё scheduler slice 100
```

Это не жёсткий пользовательский квант вроде «ровно 10 ms каждой goroutine».

Такой гарантии нет.

---

# 15. `schedtick`

`schedtick` — не CPU clock tick, не timer tick и не количество инструкций.

Это внутренний счётчик scheduler progress конкретного P.

Упрощённо:

```text
обычный запуск новой scheduler slice
↓
schedtick++
```

Цепочка через `runnext` может оставаться на одном `schedtick`.

---

# 16. Почему `runnext` опасен для fairness

Представим pathological chain:

```text
G1 → wakes G2
G2 → wakes G3
G3 → wakes G4
...
```

Если все идут через `runnext`, остальные runnable G могут ждать.

Поэтому `runnext` нельзя воспринимать как безусловный direct handoff. Нужны fairness и preemption mechanisms.

---

# 17. `sysmon` и длинный `schedtick`

System monitor отслеживает, как долго P остаётся на одном scheduler tick.

Долгий tick может означать:

```text
одна CPU-bound G выполняется слишком долго
```

или:

```text
длинная цепочка G через runnext
```

Если tick не меняется достаточно долго, runtime может запросить preemption.

Цепочка:

```text
runnext
↓
inheritTime
↓
schedtick не меняется
↓
sysmon видит длинную slice
↓
preemption
```

---

# 18. Global fairness

Local queue выгодна для scalability.

Но представим:

```text
P0 local:
G1 G2 G3 G4 ...

GLOBAL:
G100 G101 G102
```

Если P0 всегда обслуживает только local queue, global work может ждать слишком долго.

Поэтому runtime периодически учитывает global queue даже при наличии local work.

Scheduler постоянно балансирует:

```text
locality
vs
fairness
```

---

# 19. Spinning M

Представим P без local work.

Если сразу park M, работа может появиться почти сразу, и thread придётся снова будить.

Если бесконечно искать работу, CPU будет занят scheduler'ом без полезного выполнения.

Go использует промежуточное состояние:

```text
spinning M
```

Такой M временно активно ищет work.

Если work найден:

```text
spinning → running
```

Если нет:

```text
spinning → parked
```

---

# 20. Почему spinning M ограничены

Пусть `GOMAXPROCS = 64`, а реально runnable только одна goroutine.

Если десятки idle workers начнут агрессивно steal'ить, мы загрузим hardware поиском несуществующей работы.

Runtime ограничивает spinning workers.

Точный heuristic — implementation detail.

Инженерная идея проста:

> Не тратить hardware parallelism на поиск работы, которой нет.

---

# 21. `wakep()`

Когда появляется новая runnable work, возникает вопрос: будить ли ещё один OS thread?

Наивно:

```text
new G
↓
wake thread
```

Но текущий worker может сам быстро выполнить эту G.

Тогда новый thread проснётся и снова уснёт:

```text
unpark
↓
ничего не нашли
↓
park
↓
unpark
↓
park
```

Это thread state thrashing.

Runtime поэтому будит дополнительные workers консервативно, учитывая idle P и уже существующих spinning workers.

---

# 22. Work stealing

Ситуация:

```text
P0: empty
P1: empty
P2: G G G G G G G G
P3: empty
```

Без stealing P2 перегружен, остальные простаивают.

Scheduler позволяет idle P выбрать victim P и забрать часть runnable work:

```text
P0
↓
victim = P2
↓
runqsteal
↓
часть G переезжает к P0
```

---

# 23. Почему крадут batch

Если steal только одну G:

```text
steal one
↓
execute
↓
steal one
↓
execute
```

слишком часто приходится обращаться к другому P.

Batch амортизирует coordination cost:

```text
one coordination operation
↓
несколько будущих local executions
```

---

# 24. Почему work stealing не бесплатен

Stealing требует atomic synchronization, чтения состояния другого P, cache traffic и scheduler CPU time.

При очень маленьких tasks стоимость scheduling может стать заметной относительно полезной работы.

Практический вывод:

> Миллион goroutines по одной микроскопической операции может масштабироваться хуже, чем более крупные units of work.


# 24.1. `runnext` — это приоритет, а не отдельная очередь

Полезно представлять `runnext` как один privileged slot:

```text
P
├── runnext → G17
└── runq    → G42 G43 G44 ...
```

Это не queue произвольной длины.

Поэтому runtime не может построить бесконечную отдельную очередь «важных G».
В конкретный момент у P есть только один кандидат, которому даётся шанс стать
следующим.

Если slot уже занят и появляется ещё одна подходящая G, runtime должен
разрулить ситуацию через обычную queue machinery.

Такая конструкция ограничивает степень локального приоритета и не позволяет
`runnext` превратиться во вторую бесконечную priority queue.

---

# 24.2. Почему `runnext` помогает cache locality

Рассмотрим producer:

```text
G1
↓
подготовила объект
↓
сделала G2 runnable
```

Объект и связанные runtime structures могли только что использоваться текущим
core.

Если G2 быстро продолжит execution на том же P/M/core:

```text
данные вероятнее остаются в caches
```

Если G2 уйдёт далеко в global queue, а позже будет stolen другим P:

```text
cache working set может понадобиться заново подтянуть
```

Именно поэтому scheduler optimizations одновременно решают:

```text
latency
+
locality
```

а не только «справедливость очередей».

---

# 24.3. Fairness не означает строгий FIFO

Строгий FIFO выглядел бы просто:

```text
G1 стала runnable раньше G2
↓
G1 обязана выполниться раньше G2
```

Scheduler Go такого контракта не даёт.

Причины:

- `runnext`;
- local queues;
- global queue;
- work stealing;
- timers;
- netpoller;
- GC/runtime work;
- preemption.

Строгий FIFO ухудшил бы возможность оптимизировать locality и быстро
реагировать на runtime events.

Поэтому fairness здесь означает скорее:

> Scheduler должен предотвращать патологическое бесконечное голодание work,
> сохраняя при этом throughput и locality.

---

# 24.4. Почему global queue нужна даже при хороших local queues

Local queue решает scalability problem, но создаёт isolation problem.

Пусть:

```text
P0.runq = 100 G
P1.runq = 0
P2.runq = 0
P3.runq = 0
```

Stealing исправляет это.

Но есть и другая ситуация:

```text
P0.runq постоянно пополняется
GLOBAL содержит старую work
```

Если P0 смотрит только local queue, global work может слишком долго не
получать execution.

Поэтому scheduler периодически учитывает global queue даже когда local work
существует.

Это tradeoff:

```text
local queue → cheap/local
global queue → coordination/fairness
```

---

# 24.5. Spinning M как latency optimization

Почему idle M не park'ится мгновенно?

Потому что park/unpark OS thread сам имеет цену.

Сценарий:

```text
t0: work закончилась
t0+2µs: появляется новая G
```

Если M мгновенно park:

```text
park
↓
новая G
↓
unpark thread
↓
OS scheduler
↓
execution
```

Если M короткое время spinning:

```text
новая G
↓
spinning M быстро находит её
↓
execution
```

Latency ниже.

Но если work не появится:

```text
spin = бесполезный CPU burn
```

Поэтому runtime ограничивает количество и длительность spinning workers.

---

# 24.6. Почему нужен строгий protocol вокруг `nmspinning`

В scheduler есть глобальная проблема гонки.

Представим:

```text
M1 решил: work нет, сейчас park
```

Одновременно:

```text
G1 создаёт новую runnable G
```

Если producer увидит:

```text
"spinning worker ещё существует"
```

и решит никого не будить, а M1 сразу после этого перестанет spinning и park,
новая work может остаться без активного worker.

Поэтому transitions между:

```text
spinning
idle
wake
```

нуждаются в аккуратном atomic protocol.

Это та же общая тема runtime:

> Wait/wakeup correctness почти всегда сложнее, чем кажется из user code.

---

# 24.7. Work stealing — это load balancing, а не бесплатное ускорение

Пусть каждая stolen G выполняет 100 ms полезной CPU work.

Stealing cost почти незаметна.

Пусть каждая G выполняет 100 ns работы.

Тогда:

```text
victim selection
atomic queue operations
cache traffic
scheduler bookkeeping
```

могут стать сопоставимы с полезной работой.

Поэтому granularity задач влияет на эффективность scheduler.

Goroutine дешёвая — это не обещание:

> любая микроскопическая операция должна быть отдельной goroutine.

---

# 24.8. Scheduler latency и tail latency backend

Пусть request уже получил все данные от PostgreSQL и network больше не ждёт.

G стала:

```text
RUNNABLE
```

Но CPU перегружен.

Она ждёт 20 ms до:

```text
RUNNING
```

Эти 20 ms пользователь видит как request latency, хотя:

```text
DB fast
network fast
lock contention нет
```

Под высоким CPU saturation scheduler queueing становится частью p95/p99.

Именно поэтому latency backend нельзя объяснять только внешним I/O.

### Что важно запомнить

- `runnext` — latency/locality optimization.
- `inheritTime` позволяет цепочке G делить scheduler slice.
- `schedtick` — внутренний progress counter.
- Spinning M уменьшает latency появления work, но тратит CPU.
- `wakep` ограничивает thread thrashing.
- Work stealing исправляет дисбаланс local queues, но имеет собственную цену.

---

# 25. `sudog`: зачем runtime ещё одна структура

У нас уже есть `G`.

Почему нельзя положить `*g` прямо в channel wait queue?

Потому что связь:

```text
G ↔ synchronization object
```

many-to-many.

---

# 26. Many-to-many

Один channel может иметь много waiters.

А одна G может одновременно ждать несколько объектов.

Главный пример:

```go
select {
case <-ch1:
case ch2 <- value:
case <-ch3:
}
```

Одна G логически зарегистрирована сразу в нескольких wait queues.

Поэтому runtime вводит отдельный waiter node — `sudog`.

---

# 27. Модель `sudog`

Упрощённо:

```text
sudog
├── g    → waiting G
├── next
├── prev
├── elem → data involved in operation
└── additional wait metadata
```

Получается:

```text
channel.recvq
↓
sudog
↓
G
```

или:

```text
semaphore wait structure
↓
sudog
↓
G
```

---

# 28. Почему `sudog` называется pseudo-g

`sudog` — вспомогательный объект, представляющий G внутри конкретной wait structure.

Он не является второй goroutine и не имеет собственного user stack.

Удобная ментальная формула:

```text
G = кто ждёт

sudog = где и в каком wait protocol эта G представлена
```

---

# 29. `select` показывает смысл лучше всего

```go
select {
case x := <-ch1:
    use(x)
case ch2 <- value:
case <-ctx.Done():
}
```

Концептуально:

```text
             G42
          /   |   \
        sg1  sg2  sg3
         |    |    |
       ch1   ch2  ctx.Done channel
```

Один case выигрывает. Остальные wait registrations надо корректно удалить.

---

# 30. Пул `sudog`

Blocking operations частые.

Если на каждый Mutex/channel/select wait делать обычный heap allocation waiter object, runtime создаст лишний allocation pressure.

Поэтому `sudog` переиспользуются через специальные runtime pools:

```text
acquireSudog
↓
использование
↓
releaseSudog
↓
pool
```

Есть локальное caching рядом с P и central runtime pool.

Точные структуры — implementation detail.

---

# 31. От channels к runtime semaphore

Для channel wait queue всё понятно:

```text
hchan.recvq
hchan.sendq
```

Но `sync.Mutex` должен оставаться маленьким.

Современный mutex концептуально:

```text
state int32
sema  uint32
```

Там нет `[]*sudog` и полноценной queue object.

Где живут waiters?

---

# 32. Runtime semaphore

`sync` использует runtime semaphore machinery как sleep/wakeup primitive.

Важно:

> Это внутренняя Go runtime abstraction, а не отдельный kernel semaphore на каждый Mutex.

Mutex использует `m.sema` как адрес/ключ для semaphore wait machinery.

---

# 33. `semacquire1`

Когда goroutine должна уснуть на semaphore:

```text
fast check
↓
ресурса нет
↓
slow path
```

Runtime должен зарегистрировать waiter и не потерять wakeup.

Упрощённо:

```text
check semaphore
↓
register that waiter exists
↓
recheck semaphore
↓
если всё ещё unavailable:
    queue sudog
    park G
```

Recheck принципиален.

---

# 34. Зачем recheck

Плохой алгоритм:

```text
G1 checks: value == 0
↓
G2 releases semaphore
↓
G1 registers waiter
↓
G1 sleeps
```

Wakeup произошёл до регистрации.

G1 может заснуть навсегда.

Поэтому wait protocol закрывает lost wakeup window.

---

# 35. `semaRoot`

Runtime не создаёт отдельную тяжёлую wait queue внутри каждого Mutex.

Вместо этого существует semaphore table.

Адрес semaphore выбирает один из `semaRoot`:

```text
&mutexA.sema ─┐
&mutexB.sema ─┼─ mapping ─► semaRoot
&mutexC.sema ─┘
```

В текущей реализации таблица имеет фиксированное число roots; конкретный размер — implementation detail.

---

# 36. Что находится в `semaRoot`

Упрощённо:

```text
semaRoot
├── lock
├── waiter structure/tree
└── waiter count
```

Несколько разных semaphore addresses могут попасть в один root.

Поэтому внутри root runtime должен различать waiters по конкретному адресу semaphore.

---

# 37. Почему таблица shared

Если бы каждый `Mutex` содержал полноценную wait queue:

```text
Mutex
├── state
├── queue lock
├── head
├── tail
├── metadata
...
```

каждый mutex стал бы существенно тяжелее.

Но большинство mutex большую часть времени uncontended.

Go оптимизирует common case:

```text
маленький Mutex
+
дорогая machinery только при contention
```

---

# 38. `Mutex` и `sudog`

При contention:

```text
G
↓
Mutex.lockSlow
↓
runtime_SemacquireMutex(&m.sema)
↓
semacquire1
↓
sudog
↓
semaRoot wait structure
↓
gopark
```

Wakeup:

```text
Unlock
↓
runtime_Semrelease
↓
найти sudog
↓
ready G
↓
WAITING → RUNNABLE
```


# 38.1. Поля `sudog`: что полезно понимать концептуально

Реальный `sudog` содержит больше служебных полей, но для mental model важны:

```text
sudog
├── g         → какая G ждёт
├── next/prev → связь внутри wait structures
├── elem      → данные channel operation
├── waitlink  → связь ожиданий одной G
└── metadata  → protocol-specific state
```

Не все поля используются одинаково всеми primitives.

Главное:

> `sudog` — не generic business object, а runtime node конкретного wait protocol.

---

# 38.2. Почему G может иметь несколько `sudog`

`select`:

```go
select {
case x := <-a:
	use(x)
case b <- y:
case <-c:
}
```

Если ничего не ready:

```text
G
├── sgA → a.recvq
├── sgB → b.sendq
└── sgC → c.recvq
```

Но физически G должна park только один раз.

Поэтому runtime должен:

1. зарегистрировать все cases;
2. park G;
3. при событии определить winner;
4. удалить losing wait registrations;
5. вернуть `sudog` в pool.

Это хороший пример, почему `sudog` нельзя просто заменить одним полем wait state
внутри G.

---

# 38.3. Почему `sudog` pooling важен именно на hot synchronization path

Представим сервер:

```text
100 000 channel operations/s
```

Если каждый blocking transition делал:

```text
malloc sudog
↓
GC later
```

получили бы дополнительный:

- allocation rate;
- GC pressure;
- cache traffic.

Runtime вместо этого старается переиспользовать waiter nodes.

Получается:

```text
acquireSudog
↓
use
↓
releaseSudog
↓
cache/pool
```

Важный вывод:

> Даже internal synchronization metadata проектируется как high-frequency
> performance-sensitive path.

---

# 38.4. `semaRoot` и адрес semaphore

`sync.Mutex` содержит маленькое поле:

```text
sema uint32
```

Но wait queue лежит не внутри этого поля.

Адрес semaphore используется runtime как identity:

```text
&m.sema
↓
runtime semaphore table
↓
root/bucket
↓
waiters именно этого semaphore
```

Это позволяет тысячам Mutex использовать общую infrastructure, не увеличивая
размер каждого объекта.

---

# 38.5. Почему shared semaphore table сама не должна стать одной global lock

Если бы существовал:

```text
ONE GLOBAL SEMAPHORE QUEUE LOCK
```

то contention на разных независимых Mutex начал бы конфликтовать на одном
runtime lock.

Поэтому semaphore machinery shard'ится/распределяется по table roots.

Общая идея та же, что позже увидим в application sharding:

```text
одна global synchronization point
↓
плохо масштабируется

несколько independent buckets
↓
меньше unrelated contention
```

---

# 38.6. Recheck закрывает race между token и waiter registration

Самая важная логика semaphore acquire:

```text
проверил token → нет
```

Этого недостаточно.

Между check и sleep release может вернуть token.

Поэтому protocol должен сделать waiter видимым и повторно проверить state.

Упрощённо:

```text
fast acquire failed
↓
register waiter presence
↓
recheck semaphore
├── token появился → consume, не park
└── token нет      → enqueue + park
```

Так runtime избегает сценария:

```text
release уже произошёл
↓
waiter ещё не был виден
↓
waiter заснул навсегда
```

---

# 38.7. Runtime semaphore и OS futex-like идеи

Концептуально runtime semaphore решает ту же инженерную задачу, что многие
low-level primitives:

```text
быстрый user-space check
↓
если ресурс доступен → не sleep
↓
если нет → зарегистрировать waiter
↓
park
↓
wake on release
```

Но конкретная Go implementation интегрирована с goroutine scheduler.

Цель — park G, а не обязательно OS thread.

Именно поэтому semaphore path органично связывается с:

```text
sudog
gopark
goready
```

---

# 38.8. Почему `sudog` может ссылаться на stack

Для channel operation blocked sender может ждать с value, лежащим на stack:

```go
value := makeThing()
ch <- value
```

Runtime должен сохранить связь:

```text
sudog.elem → location involved in operation
```

Если stack G переместится при growth, такие ссылки нельзя забыть.

Так `sudog` соединяет две большие темы:

```text
synchronization
+
movable goroutine stack
```

Именно поэтому stack relocation — не простой `memcpy`.

### Что важно запомнить

- `sudog` представляет G в конкретной wait structure.
- Many-to-many relation делает отдельный waiter object необходимым.
- `sudog` pool уменьшает allocation pressure.
- Mutex не хранит полноценную wait queue внутри себя.
- Semaphore table и `semaRoot` выносят тяжёлую wait machinery из каждого Mutex.
- Runtime semaphore — parking primitive, а не обычный application-level counting semaphore.

---

# 39. Побитовое устройство `sync.Mutex.state`

Текущая implementation использует:

```text
31                                3 2 1 0
┌──────────────────────────────────┬─┬─┬─┐
│          waiter count            │S│W│L│
└──────────────────────────────────┴─┴─┴─┘
```

Где:

```text
L = mutexLocked
W = mutexWoken
S = mutexStarving
```

Старшие биты кодируют waiter count.

---

# 40. Почему всё упаковано в один `int32`

Можно представить более очевидную структуру:

```go
type MutexState struct {
    locked   bool
    woken    bool
    starving bool
    waiters  int
}
```

Но тогда изменение нескольких связанных полей требует дополнительной synchronization.

Один `int32` позволяет:

```text
old state
↓
compute new state
↓
CAS(old, new)
```

То есть выполнить logical state transition одним atomic RMW.

---

# 41. `mutexLocked`

Свободный mutex:

```text
000...0000
```

Fast path:

```text
CAS(0, mutexLocked)
```

После успеха:

```text
000...0001
```

И `Lock` завершён без semaphore machinery.

---

# 42. Waiter count

Если mutex locked и один waiter зарегистрирован:

```text
waiters = 1
L = 1
```

Сдвиг waiter count начинается с bit 3.

```text
1 << 3 = 8
8 + 1 = 9
```

То есть state может быть `9`: locked + 1 waiter.

---

# 43. Waiter count — не идеальный snapshot очереди

Наивная модель:

> В bits 3+ всегда лежит точное физическое число `sudog`, прямо сейчас сидящих в semaphore queue.

Это слишком грубо.

Алгоритм делает atomic transitions вокруг wakeup/queueing.

Например state может быть изменён:

```text
waiter count--
+
Woken = true
```

до фактического завершения wakeup machinery.

`state` — часть protocol/state machine, а не диагностическая фотография queue.

---

# 44. `mutexWoken`

`W = 1` не означает, что mutex принадлежит разбуженной goroutine.

Смысл ближе к:

> Уже есть waiter/contender, которого мы активировали; не надо будить ещё одного.

---

# 45. Thundering herd

Пусть G1 держит mutex, а G2–G5 sleeping.

Наивный Unlock будит всех.

Тогда получаем:

```text
4 goroutines become runnable
↓
4 schedulings
↓
4 contenders
↓
1 winner
↓
остальные снова sleep
```

Цена:

- scheduler traffic;
- context switching;
- CAS retries;
- cache-line contention.

`mutexWoken` помогает сохранить принцип «одного уже разбудили — остальных пока не трогаем».

---

# 46. `mutexStarving`

Mutex имеет два operational modes:

```text
normal
starvation
```

В normal mode приоритет — throughput.

В starvation mode — progress/fairness старых waiters.

---

# 47. Transitional state: `Locked = 0`, `Starving = 1`

Интуитивно `mutexLocked == 0` хочется трактовать как «mutex свободен».

Но starvation handoff protocol допускает состояние:

```text
L = 0
S = 1
```

где новый contender не должен забрать mutex.

Ownership фактически зарезервирован для waiter, которому выполняется handoff.

Следовательно:

> `Mutex.state` надо читать как целую state machine.

---

# 48. Почему `Mutex` маленький

Архитектура:

```text
sync.Mutex
├── state int32
└── sema  uint32
```

Fast path использует `state`.

Slow path уходит в runtime semaphore machinery.

Это даёт:

```text
маленький object footprint
+
быстрый uncontended path
+
сложная fairness machinery только под contention
```

---

# 49. Детальный `lockSlow()`

Схематично:

```text
old = m.state

for {
    interpret old
    ↓
    maybe spin
    ↓
    compute new
    ↓
    CAS(old, new)
    ↓
    success?
      yes → continue protocol
      no  → old = current state; retry
}
```

Это atomic state machine.

---

# 50. Локальные переменные `lockSlow`

Концептуально важны:

```text
waitStartTime
starving
awoke
iter
old
```

Они описывают состояние текущего contender, а `m.state` — общее состояние mutex.

Например `awoke` — локальное знание текущей goroutine, что она участвует в Woken protocol.

---

# 51. Сначала возможен spin

Условие примерно:

```text
mutex locked
+
not starving
+
runtime считает spin разумным
```

Тогда G короткое время активно ждёт.

Если critical section очень короткая, полный path park/wake/schedule может оказаться дороже нескольких CPU cycles spin.

---

# 52. Почему нельзя spin всегда

Spin удерживает:

```text
G
M
P
CPU
```

Если lock занят долго, spin превращается в пустую трату CPU.

Поэтому runtime разрешает spinning консервативно.

Точный heuristic — implementation detail.

---

# 53. Установка `mutexWoken` во время spin

Если есть sleeping waiters и текущая G уже активно spin'ится, runtime может попытаться установить `mutexWoken`.

Смысл:

> Активный contender уже существует, Unlock не должен дополнительно будить sleeping waiter.

Это соединяет spinning и wake suppression.

---

# 54. Вычисление `new`

Если spin закончился, `lockSlow` строит новое state.

В normal mode contender пытается установить `mutexLocked`.

Если mutex занят, увеличивает waiter count.

Если текущая G ждёт слишком долго, может запросить starvation mode.

Если она была awoken, очищает `mutexWoken`.

Эти изменения упаковываются в один `new`.

---

# 55. CAS state transition

Потом:

```text
CAS(&state, old, new)
```

Если CAS не прошёл:

```text
другая goroutine изменила state
↓
наши предположения устарели
↓
прочитать новое state
↓
повторить
```

Это optimistic concurrency на уровне machine word.

---

# 56. Если mutex оказался свободен

После успешного CAS mutex может оказаться захвачен без parking.

Мы уже попали в slow path, но предыдущий owner успел unlock, пока contender анализировал state.

Slow path не обязательно означает sleep.

---

# 57. Если придётся спать

Если mutex занят:

```text
waiter registered
↓
runtime_SemacquireMutex
↓
sudog
↓
queue
↓
gopark
```

G становится `WAITING`, а M/P могут делать другую работу.

---

# 58. Почему waiter count увеличивают до parking

Если сначала уснуть, а потом зарегистрировать waiter, Unlock может не увидеть ожидающую G.

Это снова класс lost wakeup.

Protocol сначала отражает waiter в synchronization state, затем позволяет G заснуть.

---

# 59. `queueLifo`

Если G уже ждала, проснулась, проиграла lock и снова должна sleep, runtime может поставить её ближе к front queue.

Иначе старый waiter может снова и снова проигрывать newcomers.

Это локальная fairness optimization.

---

# 60. Измерение starvation

Runtime хранит `waitStartTime` и после wakeup проверяет длительность ожидания.

В текущей реализации threshold порядка 1 ms.

Это implementation detail, а не публичный контракт `sync.Mutex`.

---

# 61. После wakeup ownership ещё не гарантирован

В normal mode wake означает:

```text
WAITING
↓
RUNNABLE
```

Но mutex G ещё не получила.

Она снова участвует в competition.

Вот здесь возникает barging.

---

# 62. Barging

Сценарий:

```text
G1 owns mutex

G2 sleeping
```

G1 делает Unlock и будит G2.

Но G2 проходит:

```text
WAITING
↓
RUNNABLE
↓
scheduler queue
↓
RUNNING
```

Тем временем G3 уже RUNNING:

```text
G3 Lock()
↓
CAS
↓
wins
```

Новый активно выполняющийся contender забирает lock раньше ранее разбуженного waiter.

---

# 63. Почему barging допускается

Normal mode оптимизирует throughput.

G3 уже на CPU.

Если заставить её обязательно уступить старому waiter, добавится scheduler latency.

Для коротких critical sections иногда быстрее позволить текущему runnable code снова забрать lock.

---

# 64. Цена barging

Tail latency старого waiter может ухудшиться.

```text
G2 wakes
↓
newcomer wins
↓
G2 sleeps
↓
wakes
↓
another newcomer wins
```

Теоретически G2 может ждать очень долго.

Поэтому throughput policy нужна страховка от starvation.

---

# 65. Starvation mode

После достаточно долгого ожидания waiter может инициировать starvation mode.

Правила меняются.

Normal:

```text
Unlock
↓
wake waiter
↓
waiter competes
```

Starvation:

```text
Unlock
↓
handoff to waiter
```

Новые goroutines не должны steal lock и не должны обычным образом spin'иться — они становятся в очередь.

---

# 66. Handoff

В starvation mode ownership передаётся waiter более непосредственно через semaphore handoff machinery.

Это не значит, что Unlock прямо вызывает user code следующей goroutine.

Это protocol priority:

```text
released ownership
↓
designated waiter
↓
strong preference/progress
```

---

# 67. Выход из starvation

Starvation mode снижает throughput, поэтому runtime не хочет оставаться в нём навсегда.

Waiter, получивший mutex, может вернуть normal mode, если очередь нормализовалась.

Получаем адаптацию:

```text
low/moderate contention
→ normal throughput mode

pathological unfair contention
→ starvation mode

очередь нормализовалась
→ normal mode
```

---

# 68. `Unlock` тоже state machine

На fast path Unlock снимает `mutexLocked`.

Если после этого state простой, работа закончена.

Если есть waiters, starvation или woken protocol, нужен `unlockSlow`.

---

# 69. Wake only one

В normal mode `unlockSlow` старается разбудить одного waiter.

До фактического wake state может быть атомарно изменён:

```text
waiter count--
+
Woken = 1
```

После этого runtime semaphore делает wake.

Так другой concurrent actor видит, что один waiter уже назначен на wakeup, и не поднимает толпу.


# 69.1. Таблица нескольких характерных `Mutex.state`

Упростим младшие биты:

```text
... waiter bits ... S W L
```

Несколько полезных примеров.

Свободен:

```text
0 waiters, S=0, W=0, L=0
```

Захвачен без waiters:

```text
0 waiters, S=0, W=0, L=1
```

Захвачен, один waiter:

```text
1 waiter, S=0, W=0, L=1
```

Есть активированный waiter:

```text
waiters..., S=0, W=1, L=...
```

Starvation protocol:

```text
waiters..., S=1, ...
```

Ключевая мысль:

> Значение `state` читается как состояние целого протокола, а не как четыре
> независимых переменных.

---

# 69.2. Почему waiter count находится в тех же bits

Состояние:

```text
Locked
Woken
Starving
waiter count
```

логически связано.

Например Unlock должен принять решение:

```text
есть ли waiters?
уже кто-то woken?
starvation mode?
```

Если всё это хранить отдельно, atomic transition усложняется.

Packed word позволяет:

```text
old
↓
calculate new
↓
CAS
```

одним согласованным state change.

---

# 69.3. Что если `int32` waiter count теоретически переполнится

Под waiter count остаётся очень много значений — сотни миллионов.

Runtime не проектируется вокруг реального production-сценария с сотнями
миллионов одновременно ожидающих goroutines на одном Mutex.

Практически процесс исчерпает память и другие ресурсы значительно раньше.

Это хороший пример engineering assumption:

> Некоторые математически возможные states настолько далеко за пределами
> реального resource envelope, что отдельная expensive protection от них не
> входит в common-path design.

При этом нельзя использовать такую деталь как публичную гарантию API.

---

# 69.4. `lockSlow` можно читать как цикл пересчёта гипотезы

Вместо чтения исходника как набора битовых операций полезно видеть:

```text
1. Снять snapshot old
2. Решить, что хотим сделать
3. Построить desired new
4. CAS
5. Если CAS failed — snapshot устарел
6. Пересчитать решение
```

То есть это optimistic state machine.

Каждый CAS failure означает:

```text
между read и commit кто-то изменил mutex protocol
```

Именно поэтому код выглядит как цикл.

---

# 69.5. Локальный `awoke` и глобальный `mutexWoken`

Это два разных уровня состояния.

`mutexWoken`:

```text
глобальное состояние Mutex protocol
```

`awoke`:

```text
локальное состояние текущего contender внутри lockSlow
```

Текущая G должна помнить:

> Я являюсь contender, который уже был учтён как woken.

После успешного transition она должна корректно убрать/обновить соответствующий
global bit, чтобы Mutex state не остался в ложном состоянии.

---

# 69.6. Почему waiter может предпочесть LIFO при повторном sleep

Старый waiter уже:

```text
park
↓
wake
↓
scheduler delay
↓
проиграл barging
```

Если его всегда отправлять в самый хвост:

```text
он снова становится самым молодым
```

и может систематически проигрывать.

При повторном ожидании runtime может дать ему повышенный queue priority.

Это не универсальная гарантия FIFO/LIFO, а локальная fairness optimisation.

---

# 69.7. Timeline normal mode

```text
t0  G1 owns Mutex
t1  G2 Lock → slow path → park
t2  G1 Unlock
t3  G2 becomes RUNNABLE
t4  G3 is already RUNNING
t5  G3 Lock → wins
t6  G2 finally RUNNING
t7  G2 sees Mutex busy → waits again
```

На первый взгляд кажется несправедливым.

Но для throughput это может быть выгодно:

```text
G3 уже на CPU
```

Не нужно специально заставлять G3 sleep и ждать, пока scheduler вернёт G2.

---

# 69.8. Timeline starvation mode

После долгого ожидания политика меняется:

```text
G1 Unlock
↓
handoff preference старому waiter G2
↓
newcomer G3 не должен steal ownership
↓
G2 получает progress
```

Цена:

```text
меньше opportunistic throughput
+
больше fairness
```

Runtime динамически переключает policy вместо выбора одной стратегии навсегда.

---

# 69.9. Почему starvation threshold нельзя считать API

Даже если текущий runtime использует threshold порядка миллисекунды, прикладной
код не имеет права писать logic вроде:

```text
"через 1 ms Mutex гарантированно перейдёт в starvation mode"
```

Это implementation heuristic.

Публичная гарантия `sync.Mutex` не обещает точную fairness policy или момент
handoff.

---

# 69.10. Почему `TryLock` не превращает Mutex в polling primitive

Если приложение делает:

```go
for !mu.TryLock() {
}
```

оно фактически реализует собственный unbounded spin.

Цена:

- CPU burn;
- cache-line contention;
- отсутствие scheduler-friendly parking;
- возможно ухудшение progress owner.

`TryLock` полезен в редких algorithmic cases, но постоянный polling обычно
хуже обычного `Lock`, который умеет адаптироваться от CAS к parking.

---

# 69.11. Critical section определяет реальную capacity Mutex

Если один protected operation занимает:

```text
10 µs
```

теоретический upper bound одной serial section порядка:

```text
~100 000 операций/с
```

без учёта overhead.

Если туда случайно попало network I/O:

```text
10 ms
```

upper bound уже порядка:

```text
~100 операций/с
```

Даже 128 CPU cores не отменяют serialization.

Отсюда production-правило:

> Под lock держат invariant, а не всю бизнес-операцию.

---

# 69.12. Mutex contention и queueing theory

Если service rate critical section:

```text
µ
```

а incoming contention rate:

```text
λ
```

при:

```text
λ → µ
```

queueing delay начинает резко расти.

При:

```text
λ > µ
```

стабильной очереди уже нет — waiters накапливаются.

Mutex здесь не создаёт проблему; он делает реальный serial bottleneck видимым.

Нужно либо:

- уменьшить critical section;
- уменьшить arrival rate;
- shard state;
- изменить ownership model.

### Что важно запомнить

- `lockSlow` — CAS-driven state machine.
- Spin полезен только для короткого contention.
- Waiter регистрируется до parking.
- Wake в normal mode не означает ownership.
- Barging повышает throughput, но ухудшает fairness.
- Starvation mode меняет protocol на handoff ради progress.
- `mutexWoken` координирует active contender и sleeping waiters.

---

# 70. CPU cache hierarchy: зачем вообще спускаться ниже Go

Пока мы говорили:

```text
CAS contention
cache-line ping-pong
false sharing
```

Но откуда берётся цена?

Современный CPU выполняет арифметику значительно быстрее, чем ходит в DRAM.

Поэтому между core и RAM существует hierarchy caches.

Упрощённо:

```text
CPU registers
↓
L1
↓
L2
↓
LLC / L3
↓
RAM
```

Конкретная topology зависит от processor.

---

# 71. Latency hierarchy

Точные числа зависят от hardware, но порядок важен:

```text
register
<
L1
<
L2
<
shared LLC
<
remote cache / interconnect
<
DRAM
<
remote NUMA memory
```

Поэтому performance определяется не только количеством инструкций.

Важно, где физически находятся данные, с которыми работает core.

---

# 72. Cache line

CPU cache обычно работает не с отдельным `int64`, а блоками — cache lines.

На современных x86 распространён размер 64 bytes, но это hardware detail, а не Go guarantee.

Если прочитать один 8-byte counter, CPU фактически подтянет окружающую cache line.

---

# 73. Spatial locality

Это полезно, когда рядом лежат данные, которые скоро понадобятся.

Например:

- соседние array elements;
- fields одной структуры;
- последовательный scan.

Один cache miss может принести несколько полезных значений.

Но cache-line granularity создаёт проблему для shared mutable state.

---

# 74. Private и shared caches

Упрощённая машина:

```text
Core 0
├── L1
└── L2

Core 1
├── L1
└── L2

Core 2
├── L1
└── L2

        ↓
      LLC
        ↓
       RAM
```

Одна memory location может иметь copies в caches нескольких cores.

Для read-only data это хорошо.

Но при write нужна coordination.

---

# 75. Проблема coherence

Допустим `x = 10`, а cache line с `x` есть у Core 0, Core 1 и Core 2.

Core 0 делает `x = 11`.

Core 1 не должен бесконечно читать старое значение.

Нужен hardware protocol согласования cached copies — cache coherence.

---

# 76. Что coherence решает

Для конкретной cache line hardware согласует:

- кто имеет writable ownership;
- какие copies валидны;
- какие надо invalidate;
- как другие cores получают новую версию.

Для записи core обычно должен получить line в writable state.

---

# 77. MESI как учебная модель

Классический protocol:

```text
M — Modified
E — Exclusive
S — Shared
I — Invalid
```

Это учебная модель.

Реальные CPUs могут использовать MESIF, MOESI и vendor-specific варианты.

---

# 78. Shared

Два cores читают одну line:

```text
Core 0: S
Core 1: S
```

Оба могут читать без постоянного ownership ping-pong.

Read sharing масштабируется хорошо.

---

# 79. Exclusive

Если line есть только у одного core и соответствует memory:

```text
Core 0: E
```

Core может перейти к Modified при записи без необходимости сначала инвалидировать sharers, потому что других sharers нет.

---

# 80. Modified

Core изменил line:

```text
Core 0: M
```

Его copy содержит актуальное изменённое значение.

Другому core для доступа потребуется coherence interaction.

---

# 81. Invalid

Если другой core получил writable ownership:

```text
Core 0: M
Core 1: I
Core 2: I
```

старые copies больше нельзя использовать.

---

# 82. Hot shared write

Возьмём:

```go
var counter atomic.Int64
```

И много goroutines на разных cores:

```go
counter.Add(1)
```

Все хотят писать в одну memory location.

Writable ownership одной cache line постоянно перемещается:

```text
Core 0 owns line
↓
Core 7 wants write
↓
ownership transfer
↓
Core 7 owns line
↓
Core 3 wants write
↓
ownership transfer
...
```

Это cache-line ping-pong.

---

# 83. Почему atomic operation может быть дорогой

В Go-коде `counter.Add(1)` выглядит как одна операция.

Но hardware должен получить cache line, writable ownership, выполнить atomic RMW и согласовать copies других cores.

При contention цена определяется не арифметикой `+1`, а coordination между cores.

---

# 84. Coherence ≠ memory ordering

Cache coherence отвечает примерно на вопрос:

> Как несколько caches согласуют значения одной memory location/cache line?

Memory ordering отвечает:

> В каком порядке memory operations могут наблюдаться другими cores?

Нельзя объяснять Go Memory Model только MESI.

MESI не заменяет language-level synchronization semantics.

---

# 85. Out-of-order execution

CPU может выполнять instructions не строго в source order, если это не нарушает архитектурно допустимое поведение.

Есть pipelines, reorder buffers, store buffers и speculative execution.

Compiler тоже может менять порядок в рамках language model.

Поэтому source order не всегда равен моменту, когда другой core наблюдает memory effects.

---

# 86. Memory barriers

Barrier/fence ограничивает допустимые ordering transformations/observations.

Плохое упрощение:

> Memory barrier сбрасывает весь CPU cache в RAM.

Нет.

Barrier прежде всего задаёт ordering constraints.

Cache coherence и persistence в DRAM — отдельные вопросы.

---

# 87. Acquire / Release

Полезная mental model.

Release:

```text
предыдущие writes
↓
publish synchronization event
```

Acquire:

```text
observe synchronization event
↓
последующие reads видят соответствующую опубликованную state
```

Public Go atomics сегодня описаны сильнее: они участвуют в sequentially consistent order.

---

# 88. Go Memory Model поверх hardware

Программист пишет:

```go
mu.Lock()
shared = 42
mu.Unlock()
```

другой:

```go
mu.Lock()
fmt.Println(shared)
mu.Unlock()
```

Публичная гарантия Go задаёт synchronization ordering.

Программисту не нужно вручную выбирать x86 fence, ARM barrier или cache invalidation.

Compiler/runtime обеспечивают hardware implementation публичной model.

---

# 89. CAS

Compare-And-Swap концептуально:

```text
if memory == old {
    memory = new
    return true
}
return false
```

Операция atomic относительно других participants.

CAS используется в Mutex state, scheduler internals и lock-free state machines.

---

# 90. CAS loop

Типичный паттерн:

```go
for {
    old := state.Load()
    new := compute(old)

    if state.CompareAndSwap(old, new) {
        break
    }
}
```

CAS failure означает:

> Между нашим read и update другой participant изменил state.

Нужно перечитать state и пересчитать transition.

---

# 91. x86: `LOCK CMPXCHG`

На amd64 CAS обычно реализуется instruction sequence вокруг `LOCK CMPXCHG`.

`LOCK` не следует объяснять как «процессор каждый раз блокирует всю системную memory bus».

Для обычной aligned cacheable memory современные processors используют cache-coherence machinery и ownership cache line.

---

# 92. Почему `LOCK` всё равно дорогой

Даже без блокировки всей bus нужно:

```text
получить exclusive ownership line
↓
согласовать другие caches
↓
выполнить atomic RMW
```

Если line уже local-owned, дешевле.

Если line bouncing между cores или sockets, дороже.

---

# 93. ARM64

ARM имеет более слабую memory ordering model, чем привычная x86 mental model.

Современные ARM64 могут использовать LSE atomic instructions.

Другой классический path — exclusive load/store sequences:

```text
load-exclusive
↓
compute
↓
store-exclusive
↓
успех?
  нет → retry
```

Конкретное lowering зависит от Go version, target и hardware capabilities.

---

# 94. Exclusive monitor model

Упрощённо ARM-style CAS loop:

```text
LDXR/LDAXR
↓
прочитали old и поставили exclusive reservation
↓
вычислили new
↓
STXR/STLXR
↓
если reservation потеряна:
    retry
```

Если другой core вмешался, store-exclusive может провалиться.

Это аппаратная форма optimistic concurrency.

---

# 95. Go atomic semantics

На public уровне Go programmer ориентируется на Go Memory Model, а не opcodes.

Публичные `sync/atomic` operations ведут себя так, как если бы были упорядочены в один sequentially consistent order.

```text
Go semantics
↓
compiler/runtime implementation
↓
x86/ARM instructions
```

---

# 96. CAS не делает сложный invariant автоматически безопасным

Представим:

```text
balance
version
status
```

Если атомарно менять только `balance`, это не делает atomic transition всей тройки.

Atomics удобны, когда invariant помещается в небольшой atomic state.

Для сложного составного invariant `Mutex` часто проще и безопаснее.


# 96.1. Cache hierarchy — это ещё и вопрос bandwidth

Обычно обсуждают latency:

```text
L1 быстрее RAM
```

Но есть второй ресурс — bandwidth.

Много cores могут одновременно генерировать огромный memory traffic.

Даже если данные не contended логически, workload может упереться в:

```text
memory bandwidth
```

Поэтому CPU-bound и memory-bound workloads ведут себя по-разному.

Добавление cores помогает, пока не исчерпан следующий shared resource.

---

# 96.2. Cache locality бывает temporal и spatial

Temporal locality:

```text
использовали x
↓
скоро снова используем x
```

Spatial locality:

```text
использовали address A
↓
скоро понадобятся соседние addresses
```

Scheduler locality интересна прежде всего temporal locality рабочего набора G.

Data layout — spatial locality.

Обе влияют на performance, хотя на уровне Go-кода мы просто видим structs,
slices и goroutines.

---

# 96.3. Почему shared read обычно масштабируется лучше shared write

Пусть 32 cores читают immutable config.

Cache line может быть shared:

```text
Core0 S
Core1 S
Core2 S
...
```

Нет постоянной необходимости передавать writable ownership.

Теперь все делают write:

```text
exclusive ownership нужно каждому
```

Cache line начинает мигрировать.

Поэтому immutable/read-mostly structures часто масштабируются лучше mutable
global state.

---

# 96.4. Пример MESI transition для одного counter

Начало:

```text
Core0: S
Core1: S
```

Core0 хочет increment.

Нужно writable ownership:

```text
Core0 requests ownership
↓
Core1 copy invalidated
↓
Core0: M
Core1: I
```

Теперь Core1 хочет increment:

```text
Core1 requests line
↓
coherence transfer
↓
Core1: M
Core0: I
```

Если так происходит миллионы раз:

```text
арифметика +1
```

становится почти неинтересной по сравнению с movement cache line.

---

# 96.5. MESI не объясняет все modern CPU details

Учебная модель полезна, но реальные processors имеют:

- дополнительные coherence states;
- directories/snoop filters;
- complex interconnect;
- inclusive/non-inclusive cache policies;
- prefetchers;
- store buffers;
- architecture-specific ordering.

Поэтому MESI используют как conceptual language:

```text
shared copies
exclusive write ownership
invalidations
```

а не как точный transistor-level trace конкретного CPU.

---

# 96.6. Store buffer: почему write не обязана мгновенно стать видимой всем

Core может сначала поместить write в store buffer, продолжая execution.

Это позволяет не останавливать pipeline на каждом store.

Но другой core не обязан мгновенно наблюдать эффект в source-code order без
соответствующего synchronization contract.

Отсюда возникает необходимость memory model и ordering primitives.

---

# 96.7. Fence — это constraint, а не «команда записать RAM»

Очень распространённая ошибка:

```text
memory barrier
=
flush cache
```

Нет.

Fence ограничивает ordering memory operations согласно architecture model.

Cache coherence продолжает отдельно обеспечивать согласованность cached copies.

DRAM может вообще не участвовать непосредственно в каждой передаче данных:
новейшая line может прийти от другого cache через coherence fabric.

---

# 96.8. x86 кажется «сильным», но data race всё равно data race

x86 имеет относительно сильную ordering model по сравнению со многими ARM
scenarios.

Из этого иногда делают опасный вывод:

> На x86 без synchronization всё обычно работает.

Даже если конкретный hardware часто ведёт себя ожидаемо, Go compiler и Go
Memory Model не дают программе права зависеть от data race.

Код должен быть корректен на уровне языка, а не «случайно стабилен на моём x86».

---

# 96.9. ARM заставляет лучше видеть абстракцию memory model

На более weakly ordered architecture некоторые hardware reorderings более
очевидны.

Но прикладной Go-код не должен вручную выбирать:

```text
DMB
LDAR
STLR
```

Это задача compiler/runtime implementation.

Программист выбирает:

```text
Mutex
channel
atomic
```

и получает Go-level happens-before / synchronization semantics.

---

# 96.10. CAS и cache line ownership — две стороны одной операции

CAS обещает atomicity на language/ISA уровне.

Hardware для этого должен сериализовать конфликтующие accesses к underlying
cache line.

Поэтому:

```text
CAS semantic guarantee
```

имеет физическую цену:

```text
coherence ownership
+
serialization
+
potential retries
```

Чем больше cores одновременно спорят за одно word, тем сильнее эта цена.

---

# 96.11. Почему atomics часто быстрее Mutex при low contention

Low contention:

```text
load/CAS
↓
success
```

Нет:

- parking;
- sudog;
- scheduler transition.

Поэтому simple atomic state machine может быть очень дешёвой.

Но при high contention:

```text
CAS fail
retry
cache line moves
CAS fail
retry
```

преимущество уменьшается.

Универсального правила «atomic всегда быстрее mutex» нет.

---

# 96.12. Memory Model — это контракт между programmer, compiler и hardware

Полезная трёхслойная модель:

```text
Go source
↓
Go Memory Model
↓
compiler/runtime lowering
↓
ISA memory model
↓
hardware
```

Application correctness формулируется на верхнем уровне.

Compiler обязан подобрать такие instructions/barriers, чтобы public Go
semantics соблюдалась на конкретной architecture.

Именно поэтому переносимый concurrent code нельзя проектировать, рассуждая
только о конкретном opcode одной CPU family.

### Что важно запомнить

- CPU работает через hierarchy caches, а не напрямую с RAM на каждую инструкцию.
- Cache line — единица, на которой проявляется coherence.
- MESI — учебная модель coherence, а не Go API.
- Coherence и memory ordering — разные проблемы.
- CAS требует hardware coordination.
- На x86 и ARM mechanism различается, но Go предоставляет единый language-level contract.
- Memory barrier не означает flush всего cache в RAM.

---

# 97. Cache-line ping-pong

Есть:

```go
var requests atomic.Uint64
```

Каждый request handler:

```go
requests.Add(1)
```

На малом числе cores проблем может быть почти не видно.

На десятках cores один global counter становится hot cache line.

---

# 98. Что масштабируется плохо

Пусть:

```text
Core 0 → Add
Core 1 → Add
Core 2 → Add
Core 3 → Add
```

Логическая operation — increment.

Physical resource — одна cache line.

Она не может одновременно быть независимо writable всеми cores.

Получаем serialization через coherence ownership.

---

# 99. Lock-free ≠ scalable

Плохое упрощение:

> Mutex медленный, atomic lock-free, значит atomic масштабируется идеально.

Нет.

Можно убрать parking/scheduler overhead mutex и получить:

```text
100 cores
↓
one atomic word
↓
cache-line contention
```

Bottleneck просто переместился ниже.

---

# 100. Failed CAS тоже стоит денег

CAS loop при 20 contenders:

```text
1 winner
19 failures
```

Проигравшие делают reload, recompute и retry.

Всё это создаёт cache/coherence traffic.

Lock-free algorithm под высоким contention может масштабироваться хуже ожиданий.

---

# 101. Почему Mutex parking может помочь coherence

При сильном contention Mutex park'ит waiters.

```text
100 contenders
↓
не все активно выполняют CAS
↓
большинство WAITING
↓
active contenders меньше
```

Это снижает CPU burn, failed CAS и cache-line bouncing.

Parking нужен не только для экономии CPU, но и для уменьшения pressure на shared synchronization state.

---

# 102. False sharing

Две переменные логически независимы:

```go
type Stats struct {
    requests uint64
    errors   uint64
}
```

Core 0 меняет `requests`, Core 1 — `errors`.

Если fields находятся в одной cache line:

```text
[ requests | errors | ... same cache line ... ]
```

hardware coherence всё равно работает на line granularity.

---

# 103. Что происходит при false sharing

Core 0 хочет write `requests` и получает ownership всей line.

Core 1 хочет write `errors` и должен получить ownership той же line.

Получается:

```text
line → Core 0
line → Core 1
line → Core 0
line → Core 1
```

Хотя переменные разные.

---

# 104. Почему sharing называется false

True sharing:

```text
оба cores реально меняют одну variable
```

False sharing:

```text
variables разные
но лежат в одной coherence unit
```

Для application logic sharing нет, для hardware есть.

---

# 105. Padding

Один подход — физически разнести hot fields.

Например концептуально:

```go
type Counter struct {
    value atomic.Uint64
    _     [56]byte
}
```

Но padding нельзя превращать в cargo cult.

Причины:

- line size зависит от architecture;
- struct layout имеет нюансы;
- memory footprint растёт;
- bottleneck может быть вообще не здесь.

Padding используют после измерений.

---

# 106. Sharding

Вместо одного global counter:

```text
counter[0]
counter[1]
counter[2]
...
counter[N-1]
```

Workers обновляют разные shards.

Итог считается реже:

```text
sum(shards)
```

---

# 107. Почему sharding помогает

Было:

```text
32 cores
↓
1 cache line
```

Стало:

```text
32 cores
↓
N independent lines
```

Ownership contention распределяется.

Цена:

- больше memory;
- сложнее aggregate;
- значение не обязательно мгновенно централизовано;
- сложнее invariant.

---

# 108. Sharded lock

То же работает для data structures.

Вместо:

```text
one map
+
one mutex
```

можно использовать:

```text
shard(hash(key) % N)
↓
own map + own mutex
```

Независимые keys чаще обслуживаются разными locks.

Это уменьшает logical contention и cache-line contention.

---

# 109. Но sharding не магия

Если 90% запросов идут в один hot key, то 90% всё равно попадут в один shard.

Sharding помогает только тогда, когда workload реально распределяется.

---

# 110. NUMA

На больших multi-socket servers memory физически распределена.

Условно:

```text
Socket 0
├── cores
└── local memory

Socket 1
├── cores
└── local memory
```

Core может обращаться к local NUMA memory или remote memory через interconnect.

Remote access обычно дороже.

---

# 111. Hot state на NUMA

Если один global mutex/counter используют goroutines на cores разных sockets:

```text
Socket 0
↕
shared cache line
↕
Socket 1
```

ownership transfer проходит через межсокетный interconnect.

Цена contention становится выше, чем внутри одного socket.

---

# 112. Почему benchmark на ноутбуке может обмануть

Ноутбук часто имеет один socket и сравнительно простую topology.

Production machine может иметь несколько sockets, десятки или сотни cores и NUMA.

Global atomic, который выглядит нормально локально, может масштабироваться значительно хуже на большой машине.

---

# 113. Scheduler migration тоже имеет cache cost

Если G долго работала на одном core, её working set может быть горячим в caches.

После migration:

```text
G → другой P/M/core
```

новому core приходится снова подтянуть данные.

Поэтому locality scheduler имеет hardware цену.

Это одна из причин, почему runtime не хочет без необходимости разбрасывать связанную работу по threads.

---

# 114. Когда вообще думать про NUMA

Не когда API отвечает три секунды из-за SQL query.

Нормальный порядок диагностики:

```text
application logic
↓
I/O waits
↓
database
↓
mutex/block profiles
↓
CPU profile
↓
allocation/GC
↓
scheduler
↓
hardware counters / cache / NUMA
```

NUMA — advanced layer.

---

# 115. Диагностика cache contention

Go tools сначала показывают symptom:

```text
benchmark scaling stops
mutex profile hot
CPU profile показывает atomic loop
throughput не растёт с GOMAXPROCS
```

Дальше на Linux можно использовать `perf`, PMU counters, `perf stat`, `perf c2c` и NUMA tools.

Hardware profiling — второй этап, а не первая реакция.

---

# 116. Эксперимент: global atomic

```go
package counter

import (
    "sync/atomic"
    "testing"
)

func BenchmarkAtomic(b *testing.B) {
    var counter atomic.Uint64

    b.RunParallel(func(pb *testing.PB) {
        for pb.Next() {
            counter.Add(1)
        }
    })
}
```

Запуск:

```bash
go test -bench=BenchmarkAtomic -cpu=1,2,4,8,16
```

Не надо заранее обещать конкретную форму результата.

Смотрим throughput scaling и `ns/op`.

Если shared atomic становится bottleneck, увеличение CPU перестаёт давать пропорциональный выигрыш.

---

# 117. Эксперимент: sharding

Можно сравнить single atomic с multiple independent counters.

Цель:

> Показать, что contention существует даже без Mutex и parking.

Нужно отдельно учитывать стоимость aggregation.

---

# 118. Эксперимент: false sharing

Сравниваются два hot counters рядом в struct и вариант с физическим разделением.

Но результат зависит от CPU, scheduler placement, architecture и фоновой нагрузки.

Поэтому false sharing experiment — hardware-sensitive demonstration, а не гарантированный тест с одинаковыми цифрами на каждом ноутбуке.

---

# 119. Сквозная цепочка: от Mutex до CPU

Теперь вся тема соединяется:

```text
sync.Mutex.Lock()
↓
fast CAS
↓
contention
↓
CAS failures / spin
↓
shared Mutex.state
↓
one hot cache line
↓
coherence ownership movement
↓
cache-line ping-pong
↓
CPU scalability falls
```

Если contention сильный:

```text
lockSlow
↓
runtime semaphore
↓
sudog
↓
gopark
↓
WAITING
```

Количество active contenders уменьшается.

Получаем адаптивную стратегию:

```text
низкий contention
→ CAS fast path

короткий contention
→ spin

долгий contention
→ park

патологическая unfairness
→ starvation handoff
```

Именно это объясняет сложность `sync.Mutex`.

---

# 120. Сквозная цепочка: scheduler и locality

```text
G becomes RUNNABLE
↓
local queue / runnext
↓
same P work
↓
better locality

или

P empty
↓
work stealing
↓
G migrates
↓
load balance improves
↓
cache locality may worsen
```

Scheduler постоянно торгуется:

```text
load balancing
vs
cache locality
```

Идеального решения без компромисса нет.

---

# 121. Сквозная цепочка: atomics

```text
application wants lock-free counter
↓
atomic CAS/RMW
↓
hardware atomic instruction
↓
exclusive cache-line ownership
↓
coherence traffic
↓
contention
```

Вывод:

> «Без mutex» ещё не означает «без synchronization cost».

---


# 121.1. False sharing на структуре метрик

Возьмём:

```go
type Stats struct {
	requests atomic.Uint64
	errors   atomic.Uint64
}
```

Две goroutines:

```text
G1/Core0 постоянно increments requests
G2/Core1 постоянно increments errors
```

Логически:

```text
разные counters
```

Физически поля могут находиться в одной cache line.

Тогда каждый write требует ownership той же line.

Получаем:

```text
Core0 writes requests
↓
line owned Core0

Core1 writes errors
↓
line moves Core1

Core0 writes requests
↓
line moves Core0
```

Это false sharing: application state independent, hardware sharing real.

---

# 121.2. Padding увеличивает memory footprint

Разнести counters по cache lines можно padding'ом.

Но если у нас миллион объектов:

```text
+56 bytes padding
×
1 000 000
```

получаем десятки мегабайт лишней памяти.

То есть tradeoff:

```text
меньше coherence contention
vs
больше memory footprint / worse cache density
```

Padding оправдан только для действительно hot shared fields.

---

# 121.3. Sharding global counter

Плохая scalability:

```text
all workers
↓
one global atomic
```

Sharded:

```text
worker group 0 → counter0
worker group 1 → counter1
worker group 2 → counter2
...
```

Read total:

```text
sum(counter0...counterN)
```

Tradeoff:

```text
write scalability ↑
read/aggregation cost ↑
```

Для metrics это часто приемлемо: writes очень частые, aggregate читается реже.

---

# 121.4. Sharding Mutex-protected map

Было:

```text
one map
one mutex
```

Любые keys конфликтуют:

```text
key A
key B
key C
↓
same lock
```

После sharding:

```text
hash(key) % N
↓
shard
├── own map
└── own mutex
```

Independent keys чаще попадают в разные critical sections.

Это уменьшает logical contention и hardware contention одного `Mutex.state`.

---

# 121.5. Когда sharding не помогает

Если workload skewed:

```text
90% requests → same key
```

все всё равно попадут:

```text
same shard
```

Также sharding не решает invariant, который по природе глобальный:

```text
global strict ordering
global balance
single global uniqueness decision
```

Тогда попытка искусственно раздробить state может сделать correctness гораздо
сложнее.

---

# 121.6. NUMA: local и remote memory

На multi-socket server:

```text
Socket 0 ─ local memory 0
Socket 1 ─ local memory 1
```

Core на Socket 0 может обращаться к памяти, физически ближе к Socket 0, быстрее,
чем к remote NUMA node.

Hot global data, к которому активно обращаются оба sockets, создаёт:

```text
inter-socket traffic
```

а coherence ownership может проходить через межсокетный interconnect.

---

# 121.7. Почему contention резко хуже на большой машине

На ноутбуке:

```text
8 cores
1 socket
```

global atomic может выглядеть приемлемо.

На production server:

```text
96 cores
2 sockets
```

количество contenders выше, расстояние между ними больше, coherence traffic
дороже.

Поэтому benchmark обязательно должен учитывать target hardware class.

---

# 121.8. Goroutine migration и cache locality

G не закреплена навечно за одним core.

Если work stealing переносит G:

```text
Core0
↓
Core7
```

её data working set может остаться преимущественно в caches предыдущего core.

Новый core получает cache misses.

Это цена load balancing.

Именно поэтому scheduler не пытается бесконечно перемешивать runnable G ради
идеально равных queue lengths.

---

# 121.9. False sharing диагностика начинается не с padding

Правильная последовательность:

```text
benchmark плохо масштабируется
↓
CPU profile
↓
mutex/atomic hotspot?
↓
hardware counters / perf при необходимости
↓
подозрение на cache-line contention
↓
только потом layout/padding experiment
```

Если заранее добавить padding во все structs, легко:

- раздуть memory;
- ухудшить cache density;
- не решить реальный bottleneck.

---

# 121.10. `perf c2c` и hardware counters — последний слой

На Linux hardware performance tools могут помочь увидеть:

- cache misses;
- coherence-related events;
- contested cache lines;
- NUMA effects.

Но это уже следующий диагностический уровень.

До него нужно исключить очевидные причины:

```text
slow SQL
external API
large allocations
GC pressure
Mutex profile hotspot
scheduler backlog
```

CPU internals полезны тогда, когда обычные application/runtime profiles уже
показывают, что bottleneck действительно локальный и CPU/cache-sensitive.

---

# 121.11. Практический benchmark global atomic

```go
func BenchmarkGlobalAtomic(b *testing.B) {
	var n atomic.Uint64

	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			n.Add(1)
		}
	})
}
```

Запуск:

```bash
go test -bench=BenchmarkGlobalAtomic -cpu=1,2,4,8,16
```

Смотреть нужно не на одно число, а на scaling curve.

Идеальный linear scaling:

```text
CPU ×2
→ throughput ×2
```

для одного hot atomic обычно быстро перестаёт выполняться.

Именно график масштабирования показывает contention лучше, чем один benchmark
на default CPU count.

---

# 121.12. Практический benchmark sharded state

Можно создать N independent counters и распределить workers по ним.

Нужно сравнить:

```text
global atomic
vs
N shards
```

и отдельно учитывать стоимость чтения aggregate.

Цель:

> Показать, что уменьшение sharing может дать больше, чем оптимизация самой
> arithmetic operation.

---

# 121.13. Полная причинная цепочка hot Mutex

```text
many goroutines call Lock
↓
fast CAS fails
↓
some contenders spin
↓
Mutex.state cache line becomes hot
↓
coherence traffic grows
↓
runtime starts parking waiters
↓
active contention decreases
↓
semaphore/scheduler cost appears
↓
throughput determined by critical section capacity
```

Здесь одновременно работают:

- application design;
- runtime synchronization;
- scheduler;
- CPU cache coherence.

Вот зачем весь этот дополнительный блок изучается вместе.

---

# 121.14. Полная причинная цепочка hot atomic

```text
many goroutines
↓
one atomic word
↓
atomic RMW
↓
exclusive cache-line ownership
↓
ownership moves between cores
↓
failed retries / serialization
↓
throughput scaling flattens
```

Нет Mutex.
Нет `gopark`.
Но contention никуда не исчез.

Он просто живёт на hardware layer.

---

# 121.15. Главная граница глубины

Для обычного production incident сначала нужны:

```text
metrics
logs
trace
pprof
database diagnostics
```

И только если они приводят к:

```text
hot synchronization
CPU scaling anomaly
atomic hotspot
```

имеет смысл идти:

```text
cache line
MESI
NUMA
```

Знание CPU internals нужно не для того, чтобы любой slow request объяснять
coherence protocol.

Оно нужно, чтобы последний слой диагностики тоже оставался причинным, а не
магическим.

# 122. Что важно запомнить по всему дополнительному блоку

## Scheduler

- Lifecycle G проходит через runtime state machine.
- `newproc → runqput → findRunnable → execute` — основной runnable path.
- `gopark → park_m → WAITING` — основной blocking path.
- `goready → RUNNABLE` не означает немедленное выполнение.
- `runnext` оптимизирует latency/locality.
- Spinning M и `wakep` балансируют latency и CPU waste.
- Work stealing балансирует local queues ценой coordination и возможной потери locality.

## `sudog` и semaphore

- `sudog` — waiter representation G.
- Many-to-many relation делает отдельный waiter object необходимым.
- Waiters переиспользуются через runtime pools.
- Mutex остаётся маленьким, потому что wait queues вынесены в runtime semaphore machinery.
- `semaRoot` организует waiters по semaphore addresses.

## Mutex

- `state` — компактная atomic state machine.
- Fast path — CAS свободного mutex.
- Slow path адаптируется: spin → park.
- `mutexWoken` предотвращает лишние wakeups.
- Normal mode допускает barging ради throughput.
- Starvation mode использует handoff ради fairness.
- Конкретные bits и threshold — implementation detail текущего Go.

## CPU

- Shared mutable state имеет hardware cost.
- Cache coherence работает на cache-line granularity.
- MESI — полезная учебная модель, но не полный description современных CPUs.
- Coherence и memory ordering нельзя смешивать.
- CAS на x86 и ARM реализуется по-разному, но Go скрывает это за общей memory model.
- Memory barriers задают ordering, а не «сбрасывают весь cache в RAM».

## Performance

- Lock-free не значит contention-free.
- Hot atomic может упереться в cache-line ping-pong.
- False sharing создаёт contention между логически независимыми fields.
- Sharding распределяет mutable state, но усложняет aggregation и invariants.
- NUMA усиливает цену global hot state на больших machines.
- Hardware-level optimization делают после измерений.

---

# 123. Главная инженерная модель

Весь дополнительный блок можно свернуть в одну цепочку:

```text
Go code
↓
runtime synchronization
↓
scheduler state
↓
G / M / P
↓
atomics
↓
CPU caches
↓
cache coherence
↓
hardware topology
```

При низкой нагрузке верхние abstractions хорошо скрывают нижние слои.

Под contention abstraction начинает протекать:

```text
Mutex
↓
lockSlow
↓
semaphore
↓
parking

Atomic
↓
cache-line ownership
↓
ping-pong

Scheduler
↓
work stealing
↓
migration/locality tradeoff
```

Именно поэтому backend-разработчику полезны internals.

Не для того, чтобы вручную управлять MESI или вызывать `gopark`.

А чтобы при симптомах:

```text
CPU высокий
throughput не растёт
mutex profile горячий
GOMAXPROCS увеличили — стало хуже
```

уметь восстановить причинную цепочку и понять, на каком уровне находится bottleneck.

---

# Основные официальные источники

Детали runtime internals относятся к текущей реализации Go и могут меняться между версиями.

- Go runtime scheduler: https://go.dev/src/runtime/proc.go
- Runtime scheduler concepts (`G`, `M`, `P`): https://go.dev/src/runtime/HACKING
- `sudog`: https://go.dev/src/runtime/runtime2.go
- Runtime semaphore / `semaRoot`: https://go.dev/src/runtime/sema.go
- Current Mutex implementation: https://go.dev/src/internal/sync/mutex.go
- Go Memory Model: https://go.dev/ref/mem
- Runtime atomic implementation for amd64: https://go.dev/src/internal/runtime/atomic/atomic_amd64.s
