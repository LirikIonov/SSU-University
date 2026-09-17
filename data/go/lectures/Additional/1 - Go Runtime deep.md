# Лекция 2.1. Глубокие internals Go runtime, synchronization и CPU

> Дополнительный блок к теме **«Go runtime, concurrency и synchronization internals»**.
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
