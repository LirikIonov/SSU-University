# Лекция 2.0. Go runtime internals — расширенная версия

## О чём эта лекция

На уровне языка конкурентность в Go выглядит почти подозрительно просто:

```go
go handle(conn)
```

Одна строка — и у нас появилась ещё одна goroutine.

Но процессор не умеет выполнять goroutine. Операционная система тоже не знает, что такое goroutine. Для ОС существуют процессы, потоки, системные вызовы, файловые дескрипторы, таймеры и память.

Значит, между простым исходным кодом:

```go
go worker()
```

и реальным выполнением на CPU должен существовать слой, который отвечает как минимум на следующие вопросы:

```text
Кто представляет goroutine внутри процесса?
Кто выбирает следующую goroutine?
Кто связывает её с OS thread?
Что происходит, если goroutine ждёт сеть?
Что происходит, если OS thread застрял в syscall?
Почему 100 000 goroutine не требуют 100 000 потоков?
Как растёт stack goroutine?
Как channel блокирует goroutine, но не обязательно thread?
Как Mutex превращает contention в parking?
Что именно гарантирует synchronization с точки зрения памяти?
```

Этим слоем является **Go runtime**.

Цель лекции — построить одну непрерывную ментальную модель:

```text
Go-код
↓
goroutine
↓
G
↓
scheduler
↓
P + M
↓
OS scheduler
↓
CPU
```

и вторую, не менее важную:

```text
goroutine выполняется
↓
операция не может продолжаться
↓
channel / mutex / network / timer
↓
park
↓
WAITING
↓
событие
↓
ready
↓
RUNNABLE
↓
scheduler
↓
RUNNING
```

После этой лекции `go func()`, `<-ch`, `mu.Lock()` и `conn.Read()` должны восприниматься не как магические конструкции, а как разные входы в одну runtime-машину управления выполнением.

---

# 1. Go runtime и goroutines

## 1.1. Native binary не означает отсутствие runtime

Go компилирует программу в машинный код и обычно создаёт самостоятельный executable. Из этого иногда делают слишком сильный вывод:

> Go — native language, значит никакого runtime нет.

Runtime есть. Просто для обычной Go-программы он поставляется **вместе с binary**, а не устанавливается как отдельная виртуальная машина.

Runtime занимается механизмами, которые невозможно удобно выразить только прикладным кодом:

- созданием и завершением goroutines;
- планированием goroutines;
- управлением OS threads;
- growable stacks;
- garbage collection;
- allocation;
- timers;
- network polling;
- runtime semaphores;
- parking и wakeup goroutines;
- panic/recover;
- частью профилирования и tracing.

То есть схема процесса выглядит примерно так:

```text
┌──────────────────────────────────────┐
│             Go process               │
│                                      │
│  application code                    │
│  standard library                    │
│                                      │
│  ┌────────────────────────────────┐  │
│  │            runtime             │  │
│  │ scheduler                      │  │
│  │ GC                             │  │
│  │ allocator                      │  │
│  │ netpoller                      │  │
│  │ timers                         │  │
│  │ synchronization internals      │  │
│  └────────────────────────────────┘  │
└──────────────────────────────────────┘
                │
                ▼
              OS
```

Runtime — часть реализации Go, а не часть спецификации языка в деталях. Это важное различие: язык гарантирует семантику `go`, channel, Mutex и atomic operations, но не обещает вечную неизменность структур `g`, `m`, `p`, размеров очередей или внутреннего порядка поиска runnable goroutine.

---

## 1.2. Что такое goroutine на самом деле

Опасное упрощение:

> Goroutine — лёгкий thread.

Как первое приближение оно помогает, но быстро ломается.

OS thread — объект, которым планирует операционная система.

Goroutine — execution context, которым планирует Go runtime.

ОС не видит список goroutines и не выбирает между ними. Она планирует OS threads. Уже внутри этих threads Go scheduler multiplexes goroutines.

Условно:

```text
OS scheduler:
M1  M2  M3  M4
↓   ↓   ↓   ↓
CPU CPU CPU CPU

Go scheduler:
G1 G2 G3 G4 G5 G6 ... G100000
        ↓
        распределяются по M
```

Именно это различие позволяет количеству goroutines значительно превышать количество OS threads.

---

## 1.3. `G` — runtime-представление goroutine

В runtime goroutine представлена структурой `g`.

Не нужно запоминать все её поля. Для ментальной модели достаточно понимать, что `g` содержит данные, необходимые runtime для остановки, ожидания и последующего продолжения выполнения goroutine.

Концептуально:

```text
G
├── stack boundaries
├── saved execution context
├── status
├── current M, если G выполняется
├── scheduler metadata
├── waiting information
└── другие runtime fields
```

Важно разделить:

```text
G descriptor
```

и:

```text
goroutine stack
```

Это разные области runtime state. `g` хранит информацию о stack, но stack может быть перемещён при growth.

---

## 1.4. Состояния goroutine

Реальный runtime имеет несколько внутренних состояний и переходных вариантов. Для backend-разработчика полезна более компактная модель:

```text
RUNNABLE
RUNNING
WAITING
SYSCALL
DEAD
```

### RUNNABLE

Goroutine готова продолжать работу, но прямо сейчас CPU ей не предоставлен.

```text
может выполняться
+
ждёт scheduler
```

### RUNNING

Goroutine сейчас выполняет Go-код на некотором M, имеющем P.

### WAITING

Goroutine не может продолжить работу до события.

Причины могут быть разными:

- channel send/receive;
- Mutex/semaphore;
- timer;
- network I/O;
- `select`;
- другие runtime waits.

### SYSCALL

Goroutine вошла в системный вызов. Здесь важно различать pollable network I/O, которое runtime умеет интегрировать с netpoller, и настоящий blocking syscall, способный заблокировать OS thread.

### DEAD

Goroutine закончила выполнение. Её runtime descriptor может быть позже переиспользован.

---

## 1.5. Жизненный цикл G

Упрощённая схема:

```text
             create
               │
               ▼
           RUNNABLE
               │
          scheduler
               │
               ▼
            RUNNING
          /    |     \
         /     |      \
        ▼      ▼       ▼
   WAITING  SYSCALL   DEAD
        │      │
        │      │ event/return
        └──┬───┘
           ▼
        RUNNABLE
```

Принципиально важно различать `WAITING` и `RUNNABLE`.

Если G находится в `WAITING`, scheduler не должен давать ей CPU — она всё равно не может продолжить работу.

Если G находится в `RUNNABLE`, она уже готова работать, но может ждать своей очереди.

Это различие потом позволит понимать scheduler latency:

```text
RUNNABLE
↓
ждём CPU
↓
RUNNING
```

и blocking latency:

```text
WAITING
↓
ждём событие
↓
RUNNABLE
```

Это две разные проблемы.

---

## 1.6. Почему goroutine дешёвая

Сравнение с OS thread нельзя сводить к одному числу stack size.

Goroutine дешевле прежде всего потому, что runtime контролирует:

- создание execution context;
- небольшой growable stack;
- scheduling в user space;
- parking без обязательного parking OS thread;
- multiplexing множества G на ограниченное количество M.

Для создания новой goroutine не требуется обязательно создавать новый kernel thread.

Например:

```go
for i := 0; i < 100_000; i++ {
    go func() {
        <-make(chan struct{})
    }()
}
```

Это плохой прикладной код из-за утечки goroutines, но он хорошо демонстрирует идею: количество G может быть на порядки выше количества M.

---

## 1.7. Почему goroutine не бесплатная

Вторая опасная крайность:

> Goroutine почти ничего не стоит, значит их можно создавать без ограничений.

Каждая живая goroutine удерживает некоторый набор ресурсов:

```text
stack
+
G metadata
+
scheduler bookkeeping
+
references from stack
+
объекты, достижимые через эти references
+
возможные sudog/wait structures
+
прикладные ресурсы
```

Под прикладными ресурсами могут скрываться:

- request object;
- buffers;
- database connection;
- transaction;
- file descriptor;
- response body;
- channel references;
- cancellation tree.

Поэтому 100 000 waiting goroutines могут иметь почти нулевой CPU usage и при этом удерживать очень много памяти и ресурсов.

---

## 1.8. Goroutine leak

Goroutine leak — goroutine, которая логически больше не нужна приложению, но никогда не завершится.

Простейший пример:

```go
func leak() {
    ch := make(chan int)

    go func() {
        <-ch
        fmt.Println("done")
    }()
}
```

После выхода из `leak` sender уже никогда не появится.

Но goroutine существует:

```text
G
↓
channel receive
↓
WAITING
↓
навсегда
```

GC не может просто удалить её, потому что для runtime это корректная живая goroutine, находящаяся в ожидании.

Под нагрузкой leak превращается в accumulation:

```text
1 request → +1 leaked G
1000 requests → +1000 G
1 000 000 requests → +1 000 000 G
```

Симптомы:

- растёт `runtime.NumGoroutine()`;
- растёт heap/stack footprint;
- goroutine profile показывает множество одинаковых stacks;
- CPU при этом может оставаться низким.

### Что важно запомнить

- Goroutine — объект планирования Go runtime, а OS thread — объект планирования ОС.
- `RUNNABLE` означает «готова, но ждёт CPU»; `WAITING` — «не может продолжить без события».
- Дешёвое создание goroutine не отменяет стоимость памяти, удерживаемых объектов и внешних ресурсов.
- Goroutine leak может уничтожить сервис даже при низкой загрузке CPU.

---

# 2. G / M / P и GOMAXPROCS

## 2.1. Главный вопрос

Допустим приложение создало:

```text
100 000 goroutines
```

Машина имеет:

```text
8 CPU cores
```

Кто физически выполняет эти goroutines?

Если каждая G требует отдельный OS thread, преимущества goroutine быстро исчезают.

Здесь появляется модель **G / M / P**.

Это детали текущего устройства Go runtime, а не конструкции языка. Но без них трудно понять scheduler, syscalls и `GOMAXPROCS`.

---

## 2.2. G — goroutine

`G` отвечает на вопрос:

> Что нужно выполнить?

В `G` находится execution state конкретной goroutine.

---

## 2.3. M — machine

`M` представляет OS thread.

Он отвечает на вопрос:

> На каком kernel-scheduled thread сейчас выполняется работа?

OS scheduler не видит G. Для него существуют M.

```text
G
↓
M
↓
OS scheduler
↓
CPU
```

M может:

- выполнять user Go code;
- выполнять runtime code;
- находиться в syscall;
- быть idle;
- выполнять system-stack work.

Количество M не обязано быть равно количеству P.

---

## 2.4. P — processor runtime

Название P особенно легко понять неправильно.

P — не physical CPU и не OS thread.

P представляет набор runtime-ресурсов и право выполнять обычный user Go code.

В runtime documentation P описывается как ресурс, содержащий scheduler и allocator state, нужный M для выполнения Go-кода.

Ментальная модель:

```text
G = работа
M = OS thread
P = право + runtime resources для выполнения Go-кода
```

Чтобы M выполнял обычную G:

```text
M + P + G
```

должны встретиться.

---

## 2.5. Почему P вообще существует

Представим модель без P:

```text
G → M
```

Тогда scheduler queues, allocator caches и другие часто используемые runtime resources пришлось бы сильнее связывать с конкретными OS threads.

Но OS thread может внезапно уйти в blocking syscall.

Runtime выгодно уметь сказать:

```text
M1 застрял в kernel
↓
P0 больше не обязан ждать M1
↓
P0 передаём M2
↓
Go-код продолжает выполняться
```

То есть P позволяет отделить **runtime execution capacity** от конкретного kernel thread.

---

## 2.6. Базовая схема

```text
                Go runtime

     G1      G2      G3      G4
      │       │       │       │
      └───────┴───┬───┴───────┘
                  │ scheduler
                  ▼
             ┌─────────┐
             │ P0      │
             │ runq    │
             └────┬────┘
                  │
             ┌────▼────┐
             │ M1      │
             └────┬────┘
                  │
                  ▼
               OS CPU
```

На многопроцессорной системе одновременно существует несколько P:

```text
P0 ↔ M0 → CPU
P1 ↔ M1 → CPU
P2 ↔ M2 → CPU
P3 ↔ M3 → CPU
```

---

## 2.7. `GOMAXPROCS`

Упрощённо:

```text
GOMAXPROCS ≈ количество P
```

А значит:

> `GOMAXPROCS` ограничивает количество goroutines, которые могут одновременно исполнять обычный Go-код параллельно.

Если:

```go
runtime.GOMAXPROCS(1)
```

то runnable goroutines могут переключаться сколько угодно, но одновременно обычный Go-код выполняется через один P.

Это всё ещё concurrency:

```text
G1 runs
G1 waits
G2 runs
G2 waits
G3 runs
```

Но не parallel execution нескольких G на разных P.

---

## 2.8. `GOMAXPROCS` — не количество threads

Опасное упрощение:

```text
GOMAXPROCS = 8
→ runtime создаёт ровно 8 threads
```

Нет.

Количество M может быть больше.

Причина — threads могут блокироваться в syscalls.

Например:

```text
GOMAXPROCS = 4

P0-M0
P1-M1
P2-M2
P3-M3
```

M2 входит в blocking syscall:

```text
M2 blocked
│
└── P2 освобождается
```

Runtime может использовать другой M:

```text
P2-M4
```

Теперь M существует уже пять, хотя P по-прежнему четыре.

Следовательно:

```text
M count can be > GOMAXPROCS
```

и это нормальная работа runtime.

---

## 2.9. Concurrency и parallelism

Эти понятия особенно легко смешиваются именно в Go.

### Concurrency

Несколько операций находятся в progress.

```text
G1 выполняется
G2 ждёт сеть
G3 ждёт timer
G4 runnable
```

### Parallelism

Несколько операций физически выполняют инструкции одновременно на разных CPU.

```text
P0/M0/G1 → CPU0
P1/M1/G2 → CPU1
```

При `GOMAXPROCS=1` concurrency остаётся, parallel execution обычного Go-кода ограничен одним P.

---

## 2.10. `GOMAXPROCS` в контейнере

До Go 1.25 runtime по умолчанию в основном ориентировался на логические CPU, доступные процессу.

В контейнерной среде возникает неприятный случай:

```text
host: 64 logical CPU
container CPU limit: 2 CPU-equivalent
```

Если runtime пытается планировать Go-код так, будто доступно 64 CPU, Linux cgroup всё равно ограничивает фактический CPU budget.

Результатом может стать throttling:

```text
слишком много parallel runnable work
↓
CPU quota быстро исчерпана
↓
kernel throttles cgroup
↓
процесс временно не получает CPU
↓
p99 latency растёт
```

Начиная с Go 1.25 Linux runtime по умолчанию учитывает cgroup CPU bandwidth limit при выборе `GOMAXPROCS`, если пользователь не задал значение вручную. Runtime также умеет периодически пересматривать default при изменении доступных ресурсов.

Важно: CPU **request** и CPU **limit** — разные вещи; container-aware `GOMAXPROCS` ориентируется именно на соответствующее runtime-visible ограничение, а не на Kubernetes request как обещание scheduler'у оркестратора.

### Что важно запомнить

```text
G = goroutine execution state
M = OS thread
P = runtime execution resource
```

- Для выполнения обычного Go-кода M нужен P.
- `GOMAXPROCS` связан прежде всего с количеством P и доступным parallelism Go-кода.
- Количество M может превышать `GOMAXPROCS`.
- P позволяет runtime не привязывать execution capacity навсегда к thread, который может уйти в syscall.

---

# 3. Scheduler, run queues и work stealing

## 3.1. Что должен решить scheduler

Есть множество G:

```text
G1 G2 G3 G4 G5 ... G100000
```

Часть:

```text
WAITING
```

Часть:

```text
RUNNABLE
```

Scheduler нужен прежде всего для runnable work:

> Какую G выполнить следующей и на каком P/M?

Если использовать одну глобальную очередь всех goroutines, scheduler быстро станет общей точкой contention.

Поэтому Go использует распределённую схему.

---

## 3.2. Создание G: от `go` до RUNNABLE

Код:

```go
go worker()
```

концептуально проходит путь:

```text
go statement
↓
newproc
↓
создать или переиспользовать G
↓
подготовить initial execution context
↓
G = RUNNABLE
↓
runqput
```

`newproc`, `newproc1`, `runqput` — детали реализации runtime. Для студента важна причинная цепочка:

```text
создание goroutine
≠
создание OS thread
```

Создание goroutine прежде всего означает:

```text
создать execution context
+
сделать его runnable
+
положить в scheduler work queues
```

---

## 3.3. Local run queue

У каждого P есть своя local runnable queue.

Концептуально:

```text
P0.runq:
G1 G2 G3

P1.runq:
G4 G5

P2.runq:
G6 G7 G8 G9
```

Почему это лучше одной global queue?

### Меньше shared synchronization

Если каждый scheduler operation требует lock одной global queue:

```text
P0 ─┐
P1 ─┼→ global queue lock
P2 ─┤
P3 ─┘
```

она становится hot synchronization point.

### Локальность

P может продолжать брать работу из собственной очереди, не трогая общую структуру.

### Масштабирование

При увеличении числа P часть scheduler traffic остаётся распределённой.

---

## 3.4. Global run queue

Global queue всё равно нужна.

Она используется для глобальной координации и различных scheduler paths, в том числе когда local distribution оказывается недостаточной.

Ментальная модель:

```text
           GLOBAL RUNQ
          G20 G21 G22
              │
      ┌───────┼────────┐
      ▼       ▼        ▼
   P0.runq  P1.runq  P2.runq
```

Local queue оптимизирует common case. Global queue помогает решать fairness и redistribution.

---

## 3.5. `runnext`

Кроме normal local queue у P есть специальный `runnext` slot.

```text
P0
├── runnext → G7
└── runq    → G8 G9 G10
```

Идея:

> Некоторые goroutines выгодно запустить почти сразу вслед за текущей.

Например текущая G создаёт или делает runnable другую G, с которой причинно связана работа.

Упрощённо:

```text
G1 RUNNING
↓
G1 creates/wakes G2
↓
G2 → runnext
↓
G2 получает шанс пойти следующей
```

Это не постоянная связь между G1 и G2. Runtime не создаёт объект «родитель-потомок scheduler relationship». Это ситуативная оптимизация scheduling latency.

---

## 3.6. `inheritTime` и scheduler slice

При извлечении G из `runnext` runtime может считать её продолжением текущего scheduler time slice.

Концептуально:

```text
schedtick = N

G1
↓
G2 через runnext
↓
G3 через runnext

schedtick может оставаться N
```

Это уменьшает latency связанных goroutines, но создаёт вопрос fairness: нельзя позволить цепочке `runnext` бесконечно монополизировать P.

Поэтому runtime отслеживает progress и использует preemption machinery.

Важно:

```text
schedtick != CPU clock tick
schedtick != миллисекунда
schedtick != фиксированный quantum
```

Это внутренний scheduler progress counter P.

---

## 3.7. `schedule()` и `findRunnable()`

Когда M с P нужен следующий user G, центральная схема выглядит примерно так:

```text
schedule()
↓
findRunnable()
↓
нашли G
↓
execute(G)
```

`findRunnable` — не просто `pop()` из одной очереди.

Runtime рассматривает разные источники работы:

- local runnable work;
- global runnable work;
- timers/runtime work;
- ready network I/O;
- work других P;
- GC-related work.

Точный порядок отдельных проверок — implementation detail и может меняться.

Студенту важна архитектурная идея:

> Scheduler ищет runnable work в нескольких источниках, стараясь совместить locality, fairness и load balancing.

---

## 3.8. `execute()`

После выбора G:

```text
G: RUNNABLE
↓
execute
↓
G: RUNNING
```

G связывается с текущим M, восстанавливается её execution context, и управление передаётся user code.

Это настоящий момент, когда «готовая goroutine» снова начинает физически выполнять инструкции.

---

# 4. Work stealing, spinning и fairness

## 4.1. Почему local queues сами создают новую проблему

Local queue уменьшает contention, но может привести к дисбалансу.

```text
P0.runq: empty
P1.runq: empty
P2.runq: G1 G2 G3 G4 G5 G6 G7 G8
P3.runq: empty
```

Без дополнительного механизма:

```text
P0 idle
P1 idle
P3 idle
P2 overloaded
```

Часть CPU простаивает, хотя runnable work существует.

---

## 4.2. Work stealing

Idle P пытается получить работу у другого P.

```text
P0 empty
↓
выбирает P2 как victim
↓
забирает часть runq
↓
P0 снова имеет runnable G
```

Концептуально:

```text
до:
P0: []
P2: [G1 G2 G3 G4 G5 G6 G7 G8]

после:
P0: [G5 G6 G7 G8]
P2: [G1 G2 G3 G4]
```

Точное количество переносимых G — implementation detail. Важен принцип batch stealing.

---

## 4.3. Почему steal не по одной G

Если забирать по одной:

```text
steal
run short task
steal
run short task
steal
```

стоимость coordination начинает конкурировать с полезной работой.

Batch позволяет амортизировать cross-P interaction.

---

## 4.4. Почему нельзя бесконечно искать работу

Теперь рассмотрим обратный случай:

```text
64 P
0 runnable G
```

Если каждый idle worker бесконечно сканирует остальные P:

```text
CPU usage ≈ high
useful work = 0
```

Runtime нужен компромисс:

```text
сначала немного активно ищем работу
↓
если работы действительно нет
↓
перестаём жечь CPU
```

---

## 4.5. Spinning M

Spinning M — worker thread, который временно активно ищет работу вместо немедленного sleep.

Модель:

```text
local work? no
↓
global work? no
↓
steal? try
↓
network/timers? check
```

Зачем spin вообще нужен?

Потому что park/unpark OS thread тоже имеет стоимость.

Если новая G появится через микросекунду, иногда дешевле уже иметь активного worker, чем:

```text
park thread
↓
event arrives
↓
wake thread
↓
OS schedules thread
```

---

## 4.6. Почему spinning ограничивается

Слишком много spinning M превращает scheduler в нагрузку:

```text
32 M активно ищут работу
↓
работы нет
↓
32 CPU могут тратить cycles впустую
```

Поэтому runtime ограничивает число actively spinning workers и аккуратно координирует их parking/wakeup.

---

## 4.7. `wakep()` и thread thrashing

Наивная стратегия:

```text
появилась новая G
↓
всегда разбудить новый M
```

плоха.

Пример:

```text
G1 создаёт G2
↓
wake M2
↓
G1 сразу завершается
↓
старый M теперь тоже idle
↓
одного M снова надо park
```

Если это происходит постоянно:

```text
wake
park
wake
park
```

получается thread thrashing.

Поэтому runtime делает wakeup worker'ов консервативно: сначала учитывает idle P и уже существующих spinning workers.

---

## 4.8. Fairness и global queue

Locality сама по себе не гарантирует fairness.

Представим:

```text
P0 local queue постоянно пополняется
GLOBAL queue содержит G100
```

Если P0 всегда выбирает только local work, global G может ждать слишком долго.

Поэтому scheduler периодически учитывает global runnable work даже когда local queue не пуста.

Точные численные эвристики — implementation detail. Архитектурный конфликт такой:

```text
local-first
→ locality + scalability

periodic global check
→ fairness
```

---

## 4.9. Scheduler latency

Под нагрузкой важно различать две задержки.

### Blocking latency

```text
G = WAITING
↓
ждёт channel/network/mutex
```

### Scheduler latency

```text
G = RUNNABLE
↓
готова работать
↓
CPU пока не получила
```

При CPU saturation runnable queue может расти:

```text
incoming runnable work > available CPU execution capacity
↓
run queues растут
↓
время RUNNABLE → RUNNING увеличивается
↓
latency растёт
```

Это уже CPU/scheduler pressure, даже если goroutine нигде логически не blocked.

### Что важно запомнить

- Local queues нужны для scalability и locality.
- Global queue нужна для координации и fairness.
- Work stealing выравнивает load между P.
- Spinning уменьшает wakeup latency, но сам расходует CPU.
- Scheduler балансирует несколько конфликтующих целей: throughput, latency, fairness, locality и thread churn.

---

# 5. Netpoller, network I/O и syscalls

## 5.1. Проблема blocking API

Backend-разработчик хочет писать простой код:

```go
n, err := conn.Read(buf)
```

Логически это blocking operation:

> Пока данные не пришли, продолжать эту функцию нельзя.

Если каждый такой `Read` блокирует отдельный OS thread, то 50 000 idle network connections потребуют огромное количество threads.

Go пытается сохранить удобный blocking-style API, не превращая каждое network wait в thread wait.

---

## 5.2. OS readiness mechanisms

Современные ОС умеют сообщать:

> Этот file descriptor теперь готов к чтению/записи.

На разных платформах используются разные механизмы:

- epoll;
- kqueue;
- Windows-specific poll mechanisms;
- другие platform implementations.

Go runtime скрывает их за integrated network poller.

---

## 5.3. Что происходит при network read без данных

Упрощённо:

```text
G1 RUNNING
↓
conn.Read
↓
сейчас читать нечего
↓
runtime регистрирует интерес к fd
↓
G1 park
↓
G1 WAITING
↓
M/P могут выполнять другую G
```

То есть logical blocking остаётся:

```text
G1 действительно ждёт данные
```

но physical thread blocking не обязателен.

---

## 5.4. Возврат network event

Когда kernel сообщает readiness:

```text
packet arrives
↓
OS poller event
↓
Go netpoller
↓
waiting G становится runnable
↓
scheduler
↓
G снова RUNNING
↓
Read продолжает выполнение
```

С точки зрения прикладного кода это выглядит как обычный blocking call.

С точки зрения runtime произошло:

```text
RUNNING
→ WAITING
→ RUNNABLE
→ RUNNING
```

---

## 5.5. `pollDesc`

В runtime с pollable descriptor связан `pollDesc`, который хранит состояние read/write waiting и deadlines.

Подробные поля студенту не нужны. Важна роль:

```text
fd
↕
pollDesc
↕
waiting reader/writer G
```

Network readiness, timeout или close могут сделать ожидающую G runnable.

---

## 5.6. Netpoller встроен в scheduler

Scheduler не живёт отдельно от network events.

Когда обычные run queues пустеют или в определённых scheduler paths, runtime может проверить netpoller и получить список goroutines, которые теперь можно запускать.

То есть network I/O становится ещё одним источником runnable work:

```text
local runq
+
global runq
+
network-ready G
+
timers
+
steal
```

---

## 5.7. Blocking syscall — другой случай

Не каждый syscall можно превратить в readiness wait.

Представим:

```text
G1
↓
M1 enters kernel syscall
↓
kernel реально блокирует M1
```

Теперь M1 не может выполнять G2.

Но P не обязан навсегда оставаться за M1.

Runtime может организовать:

```text
M1 blocked in syscall
↓
P0 detached
↓
M2 acquires P0
↓
M2 executes another G
```

Именно здесь особенно хорошо видна польза P.

---

## 5.8. Возврат M из syscall

Когда M1 вернулся из kernel:

```text
M1 needs P to execute Go code again
```

Если свободного P нет, M1 не может просто начать параллельно исполнять user Go code сверх `GOMAXPROCS`.

Он должен встроиться обратно в runtime scheduling machinery.

---

## 5.9. Почему threads могут неожиданно расти

Если приложение делает много operations, которые реально блокируют OS threads:

- некоторые syscalls;
- cgo calls;
- foreign libraries;

runtime может поддерживать больше M, чтобы имеющиеся P продолжали обслуживать runnable Go work.

Поэтому ситуация:

```text
goroutines = 50 000
threads = 20
```

может быть нормальной.

Но резкий рост thread count иногда является важным диагностическим сигналом: возможно, workload ушёл из pollable Go I/O в blocking syscall/cgo territory.

---

# 6. Preemption

## 6.1. Почему cooperative blocking недостаточно

Если goroutine сама регулярно:

- ждёт network;
- ждёт channel;
- вызывает scheduler-aware operations;

scheduler легко получает управление.

Но что делать с кодом:

```go
func burnCPU() {
    for {
    }
}
```

Он не собирается добровольно ждать.

При `GOMAXPROCS(1)` без preemption такая G могла бы мешать progress остальных goroutines.

---

## 6.2. Историческая эволюция

Ранние версии Go сильнее зависели от cooperative safe points, например function calls и stack checks.

Это создавало неприятные случаи с долгими CPU loops.

Go 1.14 значительно расширил asynchronous preemption capabilities runtime.

Важно не превращать это в миф:

> С Go 1.14 любую goroutine можно остановить в абсолютно любой инструкции.

Runtime всё равно должен соблюдать безопасные условия остановки и взаимодействия с GC, stack maps и signal machinery.

---

## 6.3. Ментальная модель preemption

```text
G1 RUNNING
↓
runtime считает, что G1 слишком долго удерживает execution
↓
preemption requested
↓
G1 достигает безопасной точки остановки
↓
G1 становится доступна для rescheduling
↓
другая G получает execution
```

Для прикладного разработчика главный результат:

> CPU-bound goroutine не должна бесконечно монополизировать P только потому, что сама не вызывает blocking operation.

---

## 6.4. Scheduler не является real-time scheduler

Нельзя обещать:

```text
каждая G получает ровно 10 ms
```

или:

```text
через N миллисекунд G гарантированно будет вытеснена
```

Runtime использует эвристики и monitoring, но Go не даёт real-time scheduling guarantees.

Численные thresholds конкретной версии runtime — implementation details.

---

## 6.5. `sysmon`

В runtime существует system monitor, участвующий в наблюдении за различными conditions: timers, network polling, долго выполняющимися scheduler slices и другими задачами runtime.

Для нашей модели достаточно:

```text
P долго не демонстрирует scheduler progress
↓
runtime может запросить preemption
```

Связь с `schedtick` важна именно здесь: `schedtick` помогает runtime отличать scheduler progress от долгого удержания одного scheduling slice.

### Что важно запомнить

- Network wait и blocking syscall — принципиально разные runtime scenarios.
- Pollable I/O позволяет park G и продолжить использовать M/P.
- Реальный blocking syscall может заблокировать M, но P можно передать другому M.
- Preemption нужна для progress CPU-bound goroutines и GC, но не превращает Go в real-time runtime.

---

# 7. Stack growth и стоимость goroutine

## 7.1. Почему фиксированный большой stack не подходит

Если каждой goroutine заранее выдать большой stack, массовая concurrency быстро становится дорогой.

Например гипотетически:

```text
100 000 goroutines × 1 MiB
```

уже создают огромную потенциальную стоимость.

Но большинство backend call stacks большую часть времени довольно неглубокие.

Поэтому Go использует growable goroutine stacks.

---

## 7.2. Плохая упрощённая фраза «stack goroutine = 2 KiB»

В runtime есть минимальные размеры и исторически часто фигурирует значение 2 KiB, но современная реализация может адаптировать starting stack size, а реальный stack конкретной G растёт по мере необходимости.

Правильнее говорить:

> Goroutine начинает с относительно небольшого runtime-managed stack, который может расти и позже уменьшаться.

Не использовать 2 KiB как точную capacity-модель production сервиса.

---

## 7.3. Stack check

Compiler вставляет stack growth checks в prologue большинства Go functions.

Концептуально:

```text
function entry
↓
хватит stack для нового frame?
├── да  → function body
└── нет → morestack
```

У G есть stack bounds и guard value.

Runtime не ждёт фактического выхода за memory boundary. Growth запускается заранее.

---

## 7.4. Почему нужен guard

Представим stack, растущий в сторону меньших адресов:

```text
stack.hi
│
│ used frames
│
SP
│
│ free space
│
stackguard
│
│ runtime reserve
│
stack.lo
```

Если ждать, пока `SP` буквально выйдет за `stack.lo`, runtime уже не сможет безопасно выполнить код роста stack.

Guard создаёт запас.

---

## 7.5. `morestack` и g0

Когда stack check не проходит:

```text
user G
↓
morestack
```

Runtime сохраняет execution context и переключается на system stack текущего M — `g0`.

Почему нельзя просто продолжать работать на старом stack?

Потому что runtime собирается этот stack переместить.

Это буквально ситуация:

> Нельзя менять пол, продолжая стоять на той же доске, которую сейчас переносишь.

Модель:

```text
M
├── user G stack  ← переносим
└── g0 stack      ← runtime работает здесь
```

---

## 7.6. `newstack`

Runtime определяет новый размер и выделяет больший contiguous stack.

Рост геометрический, чтобы не копировать stack после каждой небольшой нехватки.

Учебная модель:

```text
2 KiB
↓
4 KiB
↓
8 KiB
↓
16 KiB
↓
...
```

Точные начальные значения и эвристики — implementation detail.

---

## 7.7. Почему простого `memcpy` недостаточно

Допустим stack содержит:

```go
x := 10
p := &x
```

Если `x` реально размещён на stack, `p` содержит адрес внутри текущего stack range.

Старый stack:

```text
0x100000 ... 0x102000
```

Новый:

```text
0x500000 ... 0x504000
```

После byte-for-byte copy pointer `p` всё ещё может содержать старый адрес.

Значит runtime должен не только скопировать bytes, но и скорректировать managed pointers, указывающие внутрь перемещённого stack.

---

## 7.8. Откуда runtime знает, где pointers

Compiler генерирует precise pointer metadata — stack maps.

Runtime знает, какие stack slots содержат pointers, а какие обычные scalars.

Это критично сразу для двух механизмов:

```text
GC
+
stack relocation
```

Без precise metadata пришлось бы гадать:

```text
0x100123 — pointer или просто integer?
```

Go runtime старается этого не делать.

---

## 7.9. `sudog` усложняет relocation

Позже мы увидим, что при channel wait структура `sudog` может временно содержать pointer на значение в stack G.

То есть stack может быть связан с runtime synchronization structures:

```text
sudog.elem
↓
address inside G stack
```

При copying/shrinking runtime должен учитывать такие references и синхронизироваться с channel operations.

Поэтому stack growth — это взаимодействие compiler metadata, scheduler state, GC и synchronization internals.

---

## 7.10. Продолжение функции

После relocation goroutine не стартует заново.

Runtime восстанавливает её execution context так, чтобы выполнение продолжилось корректно.

Упрощённая последовательность:

```text
function wants larger frame
↓
stack check fails
↓
morestack
↓
switch to g0
↓
allocate larger stack
↓
copy active stack
↓
adjust pointers
↓
update G stack bounds/context
↓
resume
```

Для обычного Go-кода relocation прозрачен.

---

## 7.11. Stack shrink

Stack умеет не только расти.

Например goroutine однажды ушла в глубокую recursion и выросла до большого stack, потом вернулась к небольшому call depth.

Если ничего не делать:

```text
peak stack forever retained
```

Для десятков тысяч долгоживущих G это дорого.

Поэтому runtime умеет shrink stack в безопасные моменты.

Growth срочный — без него функция не может продолжить работу.

Shrink — memory optimization, поэтому runtime может выбирать более удобный безопасный момент.

---

## 7.12. Escape analysis и большие local values

Не надо автоматически считать:

```go
var buf [1 << 20]byte
```

как «stack обязательно вырос на 1 MiB».

Compiler может принять решение разместить объект иначе, в том числе на heap, если этого требуют правила escape analysis и ограничения frame size.

Stack cost нельзя точно оценивать только глазами по исходному коду.

---

## 7.13. Реальная стоимость goroutine

Правильная capacity-модель:

```text
goroutine cost =
stack
+ G metadata
+ scheduler bookkeeping
+ GC scan work
+ retained heap graph
+ synchronization wait objects
+ external resources
```

Поэтому миллион goroutines — это не просто «примерно несколько гигабайт stack».

Они могут удерживать гораздо более дорогой object graph.

### Что важно запомнить

- Stack goroutine runtime-managed, growable и movable.
- Growth требует compiler-generated pointer maps.
- Runtime выполняет relocation на system stack `g0`.
- `stack = 2 KiB` — плохая production capacity formula.
- Стоимость G определяется не только stack, но и всем, что она удерживает.

---

# 8. Parking и wakeup goroutines

## 8.1. Главная идея runtime concurrency

Самый важный механизм всей лекции можно выразить одной фразой:

> Если goroutine не может продолжать работу, runtime старается убрать из выполнения именно G, а не обязательно заблокировать OS thread.

Это parking.

---

## 8.2. Что делает `gopark`

Упрощённо:

```text
G1 RUNNING
↓
operation cannot continue
↓
gopark
↓
G1 WAITING
↓
M/P могут выполнять другую G
```

В текущем runtime `gopark` сохраняет причину ожидания и передаёт управление в runtime parking machinery через system stack.

---

## 8.3. `park_m`

Концептуальный путь:

```text
G1 user stack
↓
gopark
↓
mcall(park_m)
↓
switch to M.g0
↓
G1: RUNNING → WAITING
↓
dropg
↓
schedule
```

Почему снова появляется `g0`?

Потому что scheduler собирается перестать исполнять текущую user G. Runtime machinery должна работать на system stack M.

---

## 8.4. `dropg`

Когда G выполняется, M и G связаны:

```text
M.curg → G
G.m    → M
```

После parking эта execution association снимается.

G остаётся существовать, но M больше не обязан ждать вместе с ней.

```text
G1 = WAITING

M1 + P0
↓
schedule
↓
G2 = RUNNING
```

Вот где буквально реализуется фраза:

> Блокируется goroutine, а thread может продолжить полезную работу.

---

## 8.5. `goready` и `ready`

Когда событие произошло:

```text
WAITING
↓
ready
↓
RUNNABLE
```

Важно:

```text
goready != немедленно RUNNING
```

`goready` означает:

> Эту G снова можно планировать.

Дальше она конкурирует за execution с другими runnable goroutines.

---

## 8.6. Полный lifecycle parking

```text
           G RUNNING
               │
               │ cannot continue
               ▼
             park
               │
               ▼
           G WAITING
               │
               │ event
               ▼
             ready
               │
               ▼
          G RUNNABLE
               │
               │ scheduler
               ▼
           G RUNNING
```

Это общий шаблон, который повторяется в:

- channels;
- Mutex slow path;
- runtime semaphores;
- network poller;
- timers;
- select.

---

## 8.7. `Gosched` — это не parking

`runtime.Gosched()` добровольно уступает execution, но G всё ещё может продолжать работу.

Модель:

```text
RUNNING
↓
Gosched
↓
RUNNABLE
```

Parking:

```text
RUNNING
↓
gopark
↓
WAITING
```

Разница фундаментальная:

```text
RUNNABLE = CPU может помочь
WAITING  = CPU сейчас не поможет
```

---

## 8.8. Preemption — тоже не parking

Preemption:

```text
G могла бы продолжать
но scheduler отдал execution другим
```

Parking:

```text
G сама логически не может продолжить без события
```

Обе ситуации прекращают текущий RUNNING interval, но причины и последующее состояние разные.

---

# 9. Lost wakeup race

## 9.1. Наивный wait protocol

Представим код runtime primitive:

```text
check condition
↓
condition false
↓
unlock
↓
park
```

Между `unlock` и `park` появляется окно.

G1:

```text
check condition → false
unlock
```

Именно сейчас G2:

```text
changes condition
wake G1
```

Но G1 ещё не parked и, возможно, ещё не зарегистрирована как waiter.

Затем G1:

```text
park
```

Событие уже произошло, wakeup потерян.

G1 может заснуть навсегда.

Это и есть **lost wakeup**.

---

## 9.2. Правильная последовательность

Нужно добиться логики:

```text
lock
↓
check condition
↓
register waiter
↓
commit to waiting
↓
release lock
↓
park
```

Ключевой принцип:

> Сначала сделай waiter видимым для стороны, которая будет будить, и только потом разрешай этой стороне изменить состояние.

Runtime primitives строятся вокруг этого инварианта.

---

## 9.3. Почему у `gopark` есть unlock callback

`gopark` умеет координировать перевод G в waiting state с release внутренней runtime-блокировки.

Это не просто удобство API.

Это способ закрыть критическое окно:

```text
unlock
↓
???
↓
park
```

и обеспечить корректную handoff-семантику между waiter registration и wakeup.

---

# 10. Channels, `sudog` и wait queues

## 10.1. Channel — не просто очередь

Упрощение:

> Channel — thread-safe queue.

Для buffered channel часть поведения действительно похожа на очередь, но channel также реализует synchronization и rendezvous semantics.

Unbuffered channel вообще не имеет пользовательского buffer элемента между sender и receiver.

Runtime representation channel — `hchan`.

Для ментальной модели важны:

```text
hchan
├── optional buffer
├── send index / recv index
├── sendq
├── recvq
├── closed state
└── internal lock
```

---

## 10.2. `sendq` и `recvq`

Если send нельзя завершить сейчас, sender может стать waiter.

Если receive нельзя завершить сейчас, receiver может стать waiter.

```text
hchan
├── sendq → waiting senders
└── recvq → waiting receivers
```

В этих queues находятся не сами `g`, а waiter structures `sudog`.

---

## 10.3. Что такое `sudog`

`sudog` представляет goroutine в конкретной synchronization wait queue.

Ментальная модель:

```text
sudog
├── g → waiting G
├── next/prev
├── operation-specific data
└── references needed for wakeup/transfer
```

Почему не положить G напрямую в очередь?

Потому что связь many-to-many.

Одна synchronization object:

```text
channel
↓
много waiting G
```

И одна G через `select` может одновременно быть зарегистрирована в нескольких wait queues:

```text
             G1
          /   |   \
        sg1  sg2  sg3
         |    |    |
       ch1  ch2   ch3
```

Отдельный waiter node решает эту задачу естественно.

---

## 10.4. Где физически живут `g` и `sudog`

`g` — runtime-managed descriptor goroutine.

Её stack находится отдельно.

Когда G RUNNABLE, на неё могут ссылаться scheduler run queues.

Когда RUNNING:

```text
M.curg → G
G.m    → M
```

Когда WAITING на channel:

```text
hchan.recvq/sendq
↓
sudog
↓
G
```

Сама G также хранит waiting-related references, необходимые runtime.

`sudog` обычно переиспользуются через runtime caches/pools, чтобы не превращать каждую короткую блокировку в обычную heap allocation.

---

## 10.5. Unbuffered receive без sender

Код:

```go
x := <-ch
```

Sender отсутствует.

Упрощённый runtime path:

```text
receiver G
↓
acquireSudog
↓
sg.g = current G
↓
register sg in recvq
↓
gopark
↓
G WAITING
```

Теперь channel содержит информацию:

> Здесь есть receiver, которого можно разбудить при следующем send.

---

## 10.6. Sender приходит позже

Другая goroutine:

```go
ch <- 42
```

Runtime видит waiting receiver:

```text
recvq not empty
↓
dequeue receiver sudog
↓
transfer value
↓
goready(receiver G)
```

Sender может продолжить выполнение без собственного parking.

Receiver становится:

```text
WAITING → RUNNABLE
```

и позже scheduler снова её запустит.

---

## 10.7. Unbuffered channel как rendezvous

Для unbuffered channel пользовательский value не обязан проходить через промежуточный queue slot.

Логическая модель:

```text
sender
  │
  │ value handoff
  ▼
receiver
```

Плюс synchronization relation, заданная Go Memory Model.

---

## 10.8. Buffered send, когда есть место

```go
ch := make(chan int, 10)
ch <- 42
```

Если buffer не полон и waiting receiver нет:

```text
lock channel
↓
copy value into circular buffer
↓
update indices/count
↓
unlock
↓
return
```

Текущая G не park'ится.

`sudog` для неё не нужен.

---

## 10.9. Buffered send, когда buffer full

Если operation blocking:

```text
buffer full
↓
operation cannot complete
↓
acquire sudog
↓
register in sendq
↓
park G
```

Позже receiver освободит место и разбудит подходящего sender.

---

## 10.10. Non-blocking channel operation

Например:

```go
select {
case ch <- value:
    fmt.Println("sent")
default:
    fmt.Println("not ready")
}
```

Если send прямо сейчас невозможен:

```text
не создавать waiter текущей G
не park
return to default branch
```

Важно сформулировать точно:

> Non-blocking operation означает, что текущая G не должна превращаться в waiting goroutine.

Но она может успешно встретить **уже существующий** `sudog` другой blocked goroutine.

Например waiting receiver уже находится в `recvq`, а non-blocking sender может выполнить rendezvous немедленно.

---

## 10.11. `select`

```go
select {
case x := <-ch1:
    _ = x
case ch2 <- 42:
case <-ch3:
}
```

Если ничего не ready, одна G может быть представлена несколькими sudog:

```text
G
├── sg1 → ch1.recvq
├── sg2 → ch2.sendq
└── sg3 → ch3.recvq
```

Когда один case выигрывает:

```text
winner event
↓
G ready
↓
остальные waiter registrations удаляются
```

Здесь становится очевидно, зачем `sudog` — отдельная структура, а не одно поле внутри G.

---

## 10.12. Channel и stack relocation

`sudog` может временно указывать на element, расположенный на goroutine stack.

Поэтому channel parking и stack copying должны быть согласованы.

Runtime source прямо содержит специальные состояния/флаги для безопасного взаимодействия stack shrinking и channel waiters.

Это важный пример того, как две абстракции встречаются внутри runtime:

```text
channel synchronization
↕
sudog
↕
stack relocation
```

### Что важно запомнить

- Channel может завершить operation immediately или park текущую G.
- `sudog` появляется не «потому что есть channel», а когда G нужно представить в wait queue.
- Non-blocking operation текущую G не park'ит.
- `select` показывает many-to-many природу `G ↔ sudog ↔ wait queues`.

---

# 11. `sync.Mutex`: fast path

## 11.1. Какую задачу решает Mutex

Mutex защищает invariant, требующий exclusive access.

Например:

```go
type Counter struct {
    mu sync.Mutex
    n  int
}

func (c *Counter) Inc() {
    c.mu.Lock()
    c.n++
    c.mu.Unlock()
}
```

На уровне API всё просто.

Но runtime должен одновременно обеспечить:

- быстрый uncontended case;
- корректный sleep/wakeup при contention;
- приемлемую fairness;
- отсутствие thundering herd;
- memory ordering guarantees.

---

## 11.2. Внутреннее представление

Текущая реализация Mutex концептуально содержит:

```go
type Mutex struct {
    state int32
    sema  uint32
}
```

Поля являются implementation detail, но архитектура полезна:

```text
state
→ логическое состояние mutex protocol

sema
→ вход в runtime semaphore/wait machinery
```

---

## 11.3. Fast path

Самый важный performance case:

```text
mutex free
↓
одна G вызывает Lock
↓
atomic CAS
↓
success
```

Концептуально:

```text
state: unlocked
↓
CAS(unlocked → locked)
↓
acquired
```

Если CAS успешен:

```text
нет sudog
нет gopark
нет semaphore wait
нет context switch
```

Именно поэтому uncontended Mutex может быть очень дешёвым.

---

## 11.4. Почему fast path должен быть маленьким

Большинство хорошо спроектированных locks должны большую часть времени быть uncontended или иметь короткую critical section.

Runtime оптимизирует common case:

```text
обычный случай → минимум инструкций
редкий contention → более сложный slow path
```

Это общий шаблон runtime design.

---

# 12. Mutex slow path, spinning и parking

## 12.1. CAS не прошёл

Если Mutex уже locked:

```text
CAS fast path
↓
failed
↓
lockSlow()
```

Теперь runtime должен решить:

```text
попробовать немного подождать активно?
или сразу park G?
```

---

## 12.2. Почему не park сразу

Представим critical section длительностью несколько десятков наносекунд/очень мало CPU work.

Если contender сразу усыпить:

```text
G park
↓
other G unlocks almost immediately
↓
wake
↓
reschedule
```

стоимость parking/wakeup может быть больше самой critical section.

Поэтому runtime иногда допускает короткий active spin.

---

## 12.3. Что такое spin

Contender остаётся RUNNING и некоторое время пытается дождаться освобождения lock.

```text
G2 RUNNING
↓
mutex locked
↓
spin briefly
↓
retry
```

Это может быть выгодно, если owner скоро unlock.

---

## 12.4. Цена spin

Spin не бесплатен.

Он удерживает:

```text
G
+
M
+
P
+
CPU
```

и может создавать atomic/cache contention.

Поэтому runtime включает spin только при подходящих условиях и ограничивает его.

Если lock удерживается долго, spin превращается в бессмысленное сжигание CPU.

---

## 12.5. Parking при contention

Если lock быстро не освободился:

```text
slow path
↓
runtime semaphore
↓
register waiter
↓
sudog/wait queue
↓
gopark
↓
G WAITING
```

Теперь CPU может использовать другая goroutine.

Это ключевой переход:

```text
active contention
↓
passive waiting
```

---

## 12.6. Низкий CPU не означает отсутствие Mutex bottleneck

Очень важный backend scenario:

```text
100 goroutines хотят один mutex
1 выполняет critical section
99 parked
```

CPU usage может быть низким.

Но throughput ограничен:

```text
one serialized critical section
```

Поэтому диагностика:

```text
CPU low
```

не означает:

```text
application has spare capacity
```

Нужно смотреть mutex/block profiles и trace.

---

# 13. `Mutex.state`: зачем там несколько битов

Для понимания slow path полезно открыть следующий слой.

В текущей implementation в `state int32` упакованы:

```text
31                                3 2 1 0
┌──────────────────────────────────┬─┬─┬─┐
│          waiter count            │S│W│L│
└──────────────────────────────────┴─┴─┴─┘

L = mutexLocked
W = mutexWoken
S = mutexStarving
```

Это implementation detail, но он хорошо показывает реальные задачи lock algorithm.

---

## 13.1. `mutexLocked`

Базовый ownership state normal mode.

Свободный Mutex:

```text
0000
```

Обычный locked:

```text
0001
```

Fast path пытается атомарно выполнить переход `0 → locked`.

---

## 13.2. Waiter count

Старшие биты учитывают waiters lock protocol.

Например один waiter означает прибавление `1 << waiterShift`.

Важно не воспринимать этот счётчик как perfect physical snapshot semaphore queue в каждый наносекундный момент. Алгоритм меняет state атомарно вокруг wakeup/handoff transitions.

---

## 13.3. `mutexWoken`

Плохое толкование:

> Woken означает, что проснувшаяся goroutine уже владеет lock.

Нет.

Смысл ближе к:

> Уже есть waiter/contender, которого мы активировали; не надо без необходимости будить ещё одного.

Зачем это нужно?

Без coordination Unlock мог бы разбудить множество waiters:

```text
G2
G3
G4
G5
↓
все RUNNABLE
↓
все CAS one hot state
↓
одна победила
остальные снова ждут
```

Получили thundering herd, scheduler work и atomic contention.

`mutexWoken` помогает ограничивать лишние wakeups.

---

## 13.4. Normal mode и barging

В normal mode пробуждённый waiter не получает Mutex автоматически.

Сценарий:

```text
G1 Unlock
↓
wake G2

пока G2 проходит WAITING → RUNNABLE → RUNNING

G3 уже RUNNING
↓
G3 calls Lock
↓
G3 wins
```

Это **barging**.

Почему runtime это допускает?

Потому что G3 уже физически выполняется. Передавать lock строго старому waiter может быть дороже по throughput.

---

## 13.5. Цена barging

Старый waiter может постоянно проигрывать новым goroutines.

```text
wake G2
↓
G3 steals
↓
G2 sleeps again
↓
wake G2
↓
G4 steals
↓
...
```

Throughput хороший, fairness плохая.

В tail latency это может стать проблемой.

---

## 13.6. Starvation mode

Текущая реализация имеет starvation mode. После достаточно долгого ожидания waiter может переключить protocol в режим, где fairness получает больший приоритет.

В current implementation threshold порядка миллисекунды; конкретное число — implementation detail.

В starvation mode новые contenders ведут себя консервативнее, а ownership передаётся ожидающим goroutines более напрямую.

Компромисс:

```text
NORMAL MODE
throughput ↑
fairness ↓

STARVATION MODE
fairness ↑
throughput может ↓
```

---

## 13.7. Почему всё упаковано в один `int32`

Можно было бы представить:

```go
type MutexState struct {
    locked   bool
    woken    bool
    starving bool
    waiters  int
}
```

Но concurrent transition тогда затрагивал бы несколько отдельных fields.

С одним integer runtime может вычислить новое состояние и попробовать атомарный CAS:

```text
old state
↓
compute new state
↓
CAS(old, new)
```

Это делает связанные state transitions единым atomic decision point.

---

# 14. `lockSlow()` как state machine

Нет необходимости читать весь runtime source построчно, но алгоритм полезно понимать концептуально.

```text
old = state
↓
расшифровать bits
↓
можно spin?
↓
вычислить desired new state
↓
CAS(old, new)
↓
CAS failed? retry with fresh state
```

Если contention требует sleep:

```text
waiter count/register state
↓
CAS succeeds
↓
runtime_SemacquireMutex
↓
park
```

Когда G проснулась в normal mode, она может снова конкурировать за lock.

То есть wakeup:

```text
не обязательно ownership
```

Это важное отличие от простого мысленного «Unlock передал lock следующему».

---

# 15. Runtime semaphore

## 15.1. Зачем Mutex ещё `sema`

`state` описывает protocol state.

Но если goroutine должна реально заснуть, нужен механизм:

```text
register waiter
↓
park G
↓
позже найти waiter
↓
ready G
```

Для этого runtime использует semaphore machinery.

---

## 15.2. Это не просто kernel semaphore

Runtime semaphore ориентирован на goroutines.

Цель:

> При contention park G, а не обязательно усыпить OS thread.

Модель acquire:

```text
try acquire logical token/state
↓
не получилось
↓
подготовить waiter
↓
проверить condition ещё раз
↓
enqueue
↓
gopark
```

Release:

```text
release state/token
↓
find waiting sudog
↓
goready
```

---

## 15.3. Зачем recheck перед sleep

Снова lost wakeup.

Между первой проверкой и регистрацией waiter condition могло измениться.

Поэтому semaphore algorithm должен согласовать:

```text
condition check
+
waiter registration
+
recheck
+
park
```

так, чтобы release не «пролетел» между ними незамеченным.

---

## 15.4. Почему wait queue не лежит прямо в каждом Mutex

Если каждый Mutex хранил полноценную queue structure:

```text
Mutex size ↑
```

даже uncontended locks платили бы memory price за механизм, который почти никогда не используют.

Runtime вместо этого использует shared semaphore table/roots, где wait structures организуются по адресу semaphore.

Концептуально:

```text
Mutex.sema address
↓
hash/select semaRoot
↓
wait structures
↓
sudog
↓
G
```

Так `sync.Mutex` остаётся маленьким.

---

## 15.5. Contention как очередь

Теперь вся картинка:

```text
G1 owns Mutex

G2 Lock
↓
fast CAS fails
↓
slow path
↓
maybe spin
↓
semaphore wait
↓
sudog
↓
WAITING

G3 Lock
↓
...
↓
WAITING

G1 Unlock
↓
state transition
↓
wake selected waiter
↓
WAITING → RUNNABLE
```

Scheduler и Mutex тесно связаны именно через park/ready, но отвечают за разные задачи:

```text
Mutex
→ кто имеет право войти в critical section

scheduler
→ когда runnable G реально получит CPU
```

---

# 16. Atomics и CAS

## 16.1. Что значит atomic

Atomic operation наблюдается как неделимая операция относительно других участников, работающих через соответствующий atomic mechanism.

Например CAS:

```text
compare memory with expected
↓
if equal
    replace with new
```

как единый read-modify-write event.

---

## 16.2. CAS loop

Типичный pattern:

```go
for {
    old := value.Load()
    next := old + 1

    if value.CompareAndSwap(old, next) {
        break
    }
}
```

Почему loop?

Потому что между:

```text
Load old
```

и:

```text
CAS
```

другая goroutine могла изменить value.

Тогда CAS корректно отказывается применять transition к уже устаревшему state.

---

## 16.3. CAS как основа state machine

Именно поэтому CAS хорошо подходит для `Mutex.state`-подобных алгоритмов:

```text
прочитать связанный state
↓
вычислить новый state
↓
применить только если никто его не изменил
```

Если кто-то изменил:

```text
retry
```

---

## 16.4. Atomic не означает дешёвый

Если одна atomic variable становится hot:

```text
Core 0 ─┐
Core 1 ─┤
Core 2 ─┼→ same atomic counter
Core 3 ─┤
Core 4 ─┘
```

CPU cores должны координировать доступ к одной cache line.

Даже failed CAS имеет стоимость.

Отсюда:

> Lock-free не означает contention-free.

Можно убрать Mutex и всё равно получить плохое scaling из-за одного hot atomic state.

---

## 16.5. Почему parking помогает ещё и atomic contention

Когда десятки contenders активно крутят CAS:

```text
CPU cycles
+
cache-coherence traffic
+
failed retries
```

Parking убирает часть contenders из активной борьбы:

```text
меньше active CAS
↓
меньше CPU waste
↓
меньше pressure on hot cache line
```

Поэтому Mutex slow path — это не просто «усыпить, чтобы не жечь CPU». Это также способ уменьшить интенсивность contention на shared atomic state.

---

# 17. Go Memory Model

## 17.1. Главный вопрос memory model

Представим:

```go
var data int
var ready bool
```

G1:

```go
data = 42
ready = true
```

G2:

```go
if ready {
    fmt.Println(data)
}
```

Интуитивное рассуждение:

> Если G2 увидела `ready == true`, значит `data` уже точно 42.

Без synchronization это неправильный способ рассуждать.

Нужно определить отношение между memory operations разных goroutines.

Для этого существует Go Memory Model.

---

## 17.2. Data race

Упрощённо data race возникает, когда две goroutines одновременно обращаются к одной memory location, хотя бы одна operation — write, и между ними нет необходимой synchronization, при этом accesses не являются корректно atomic.

Пример:

```go
var counter int

func main() {
    for i := 0; i < 1000; i++ {
        go func() {
            counter++
        }()
    }
}
```

`counter++` не является одной неделимой high-level operation:

```text
read
↓
add
↓
write
```

Несколько G могут потерять updates.

---

## 17.3. Race detector

```bash
go test -race ./...
```

или:

```bash
go run -race .
```

Race detector наблюдает реально выполненные accesses.

Он не доказывает отсутствие races в execution paths, которые тесты не активировали.

---

## 17.4. Happens-before / synchronized-before

Memory Model задаёт отношения, через которые можно гарантировать visibility и ordering.

Важно не застревать в формальной терминологии раньше времени.

Инженерная модель:

> Нужен synchronization event, который устанавливает гарантированный порядок между действиями разных goroutines.

Например Mutex:

```text
G1 writes data
↓
G1 Unlock
↓
G2 later Lock returns
↓
G2 reads data
```

`Unlock` и последующий успешный `Lock` создают необходимую synchronization relation.

---

## 17.5. Channel synchronization

Channel communication тоже создаёт ordering guarantees.

Пример:

```go
var value string
ch := make(chan struct{})

go func() {
    value = "ready"
    ch <- struct{}{}
}()

<-ch
fmt.Println(value)
```

Send/receive создают synchronization relation, поэтому чтение `value` после соответствующего receive имеет требуемую visibility guarantee.

Это важный смысл channel:

> Channel передаёт не только value. Он ещё и участвует в synchronization ordering.

---

## 17.6. Goroutine creation

Go Memory Model также задаёт ordering для goroutine start.

Если перед `go f()` текущая goroutine сделала запись, start новой goroutine находится после самого `go` statement в соответствующем synchronization sense.

Но завершение goroutine само по себе не является автоматическим сигналом другим goroutines.

Плохой код:

```go
var value string

go func() {
    value = "done"
}()

fmt.Println(value)
```

Здесь нет synchronization, гарантирующей, что write завершившейся G уже наблюдаем.

Нужно `WaitGroup`, channel, Mutex или другой synchronization primitive.

---

## 17.7. Atomics в Go

Operations из `sync/atomic` имеют сильные ordering semantics. Если эффект atomic operation A наблюдается atomic operation B, memory model задаёт synchronization relation; атомики программы ведут себя как единый sequentially consistent order.

Для backend-разработчика это полезнее запоминать так:

> Public Go atomics — не «просто CPU instruction без ordering». Они имеют определённые language-level memory guarantees.

---

## 17.8. Atomic field не защищает composite invariant

Представим:

```go
type Account struct {
    balance atomic.Int64
    version atomic.Int64
}
```

Каждое поле обновляется атомарно.

Но invariant:

```text
balance и version должны измениться как одна логическая transaction
```

из этого не следует.

Другая goroutine может увидеть:

```text
new balance
old version
```

если protocol не обеспечивает совместный invariant.

Atomicity отдельных words не равна atomicity бизнес-состояния.

---

## 17.9. Mutex или atomic?

Atomic подходит, когда invariant действительно локален и прост:

```text
counter
flag
pointer publication
small state machine
```

Mutex естественнее, когда нужно защищать:

```text
несколько связанных fields
сложный invariant
map + counters
state transitions с несколькими шагами
```

Главный критерий — correctness и ясность protocol, а не стремление любой ценой избежать lock.

---

## 17.10. Почему memory model важен backend-разработчику

Большинство backend races появляются не в красивых low-level lock-free algorithms, а в обычном application state:

- in-memory cache;
- shared map;
- mutable configuration;
- lazy initialization;
- counters/metrics wrappers;
- connection/session state;
- background refresh;
- shutdown flags.

Рассуждение «обычно запись уже успевает» не является synchronization strategy.

### Что важно запомнить

- Data-race-free Go programs получают сильную последовательную модель поведения.
- Channel, Mutex и atomics нужны не только для mutual exclusion, но и для memory ordering/visibility.
- Atomic отдельного field не защищает composite invariant.
- Если synchronization protocol трудно объяснить, lock-free вариант обычно не стал проще только потому, что в коде меньше `Mutex`.

---

# 18. Собираем механизм целиком

Теперь можно связать все части лекции в несколько типовых paths.

## 18.1. Создание goroutine

```text
go worker()
↓
create/reuse G
↓
G RUNNABLE
↓
local/global scheduler structures
↓
findRunnable
↓
execute
↓
G RUNNING on M + P
```

OS thread отдельно не создаётся на каждую G.

---

## 18.2. Channel receive без данных

```text
G RUNNING
↓
<-ch
↓
no data / no sender
↓
acquire sudog
↓
register in recvq
↓
gopark
↓
G WAITING
↓
M/P execute another G
```

Позже:

```text
sender arrives
↓
dequeue sudog
↓
transfer value
↓
goready
↓
G RUNNABLE
↓
scheduler
↓
G RUNNING
```

---

## 18.3. Mutex contention

```text
G2 Lock
↓
fast CAS fails
↓
slow path
↓
maybe spin
↓
still contended
↓
semaphore wait
↓
sudog / wait structure
↓
gopark
↓
WAITING
```

Owner:

```text
Unlock
↓
state transition
↓
selected waiter ready
↓
RUNNABLE
```

---

## 18.4. Network wait

```text
G1 conn.Read
↓
no data
↓
register fd with poller
↓
park G1
↓
WAITING

kernel says fd ready
↓
netpoller
↓
ready G1
↓
RUNNABLE
↓
scheduler
```

---

## 18.5. Blocking syscall

```text
G1 on M1/P0
↓
blocking syscall
↓
M1 stuck in kernel
↓
P0 detached/reused
↓
M2 + P0 execute other G
```

Здесь G не просто ждёт runtime event; kernel thread реально заблокирован, поэтому runtime компенсирует потерянный M через P/M management.

---

# 19. Что меняется под нагрузкой

## 19.1. CPU-bound workload

Если runnable work существенно больше CPU capacity:

```text
run queues ↑
↓
scheduler latency ↑
↓
request latency ↑
```

Создать ещё goroutines не значит увеличить throughput.

После насыщения CPU они превращаются в additional queueing.

---

## 19.2. I/O-bound workload

Много waiting network goroutines может быть нормальным:

```text
G count high
CPU moderate
threads comparatively low
```

Это одна из сильных сторон Go model.

Но каждая waiting request всё равно удерживает memory и application state.

---

## 19.3. Mutex contention

При одном hot lock:

```text
more concurrency
↓
more contenders
↓
serialization remains
↓
parking/wakeup + atomic traffic ↑
```

Throughput может перестать расти или даже ухудшиться.

---

## 19.4. Unbounded concurrency

Очень важная граница runtime abstraction:

```go
for req := range requests {
    go process(req)
}
```

Runtime может технически обслужить огромное количество G.

Но scheduler не знает, что:

```text
DB pool = 20
external API quota = 100 req/s
memory budget = 1 GiB
```

Поэтому scheduler не является backpressure mechanism.

Если входящий поток выше downstream capacity:

```text
requests
↓
more goroutines
↓
waiting for same limited resource
↓
memory and latency accumulate
```

Backpressure должен проектироваться на application/resource layer.

---

# 20. Диагностика runtime behaviour

## 20.1. `runtime.NumGoroutine`

Полезный первый индикатор:

```go
fmt.Println(runtime.NumGoroutine())
```

Само большое число ещё не проблема.

Нужно смотреть trend и причины ожидания.

---

## 20.2. Goroutine profile

Показывает stacks goroutines.

Особенно полезен при leak:

```text
50000 goroutines
все стоят в одном channel receive
```

Это уже гораздо информативнее одного counter.

---

## 20.3. Block profile

Помогает увидеть, где goroutines теряют время на blocking synchronization operations.

Важно интерпретировать его как cumulative waiting signal, а не просто список «плохих строк кода».

---

## 20.4. Mutex profile

Показывает contention вокруг Mutex/RWMutex.

Сценарий:

```text
CPU низкий
latency высокий
mutex profile показывает один hot lock
```

указывает совсем на другой bottleneck, чем CPU saturation.

---

## 20.5. `go tool trace`

Trace позволяет увидеть execution timeline и scheduler events:

- goroutine creation;
- runnable/running transitions;
- blocking;
- network waits;
- syscalls;
- GC;
- scheduler behaviour.

Это особенно полезный инструмент для этой лекции, потому что делает невидимый runtime lifecycle наблюдаемым.

---

## 20.6. Scheduler debug output

Runtime имеет `GODEBUG` scheduler tracing facilities, например `schedtrace`/`scheddetail`, позволяющие получать агрегированную информацию о scheduler state.

Это implementation diagnostics, поэтому формат и детали могут меняться между Go releases.

---

## 20.7. Race detector

```bash
go test -race ./...
```

Нужен для memory synchronization bugs, а не для поиска обычного lock contention.

Важно правильно выбирать инструмент:

```text
race detector
→ correctness of concurrent memory access

mutex/block profile
→ waiting/contention

trace
→ scheduling lifecycle/timeline

CPU profile
→ where CPU time goes
```

---

# 21. Границы гарантий и implementation details

Во всей теме важно постоянно разделять четыре уровня.

## 21.1. Гарантии языка и standard library

Например:

- semantics goroutine start;
- channel send/receive behaviour;
- synchronization guarantees;
- Mutex API contract;
- Go Memory Model.

На эти свойства прикладной код может опираться.

---

## 21.2. Runtime implementation

Например:

- структуры G/M/P;
- `runnext`;
- внутренние queue sizes;
- конкретный порядок `findRunnable`;
- `mutexWoken` и `mutexStarving` bits;
- текущие starvation thresholds;
- exact stealing heuristics.

Они полезны для понимания и диагностики, но не являются публичным language contract.

---

## 21.3. Распространённое наблюдаемое поведение

Например:

```text
waiting network G обычно не удерживает dedicated OS thread
```

Это фундаментальное свойство current implementation strategy, но конкретный low-level path зависит от platform и descriptor type.

---

## 21.4. Учебная модель

Иногда мы сознательно сокращаем детали:

```text
RUNNING → WAITING → RUNNABLE
```

Реальный runtime имеет дополнительные transient states, locks, flags, GC interactions и tracing hooks.

Хорошая учебная модель должна объяснять механизм, но не притворяться полной копией `runtime/proc.go`.

---

# 22. Финальная ментальная модель

Главная схема Go concurrency:

```text
                         Go runtime

                            G
                            │
                     runnable work
                            │
                            ▼
                      scheduler
                            │
                    ┌───────┴───────┐
                    ▼               ▼
                    P               P
                    │               │
                    M               M
                    │               │
                    ▼               ▼
                   CPU             CPU
```

Если G может продолжать работу:

```text
RUNNABLE
↓
scheduler
↓
RUNNING
```

Если не может:

```text
RUNNING
↓
channel / mutex / network / timer
↓
waiter registration
↓
gopark
↓
WAITING
```

После события:

```text
event
↓
goready
↓
RUNNABLE
↓
scheduler
↓
RUNNING
```

Для Mutex добавляется:

```text
fast CAS
↓ success
critical section

↓ failure
slow path
↓
spin or park
↓
runtime semaphore
```

Для network I/O:

```text
pollable fd
↓
netpoller
↓
park G without dedicating thread
```

Для blocking syscall:

```text
M blocked
↓
P moves to another M
```

Для memory visibility:

```text
program order alone between different G недостаточен
↓
channel / mutex / atomic synchronization
↓
happens-before guarantees
```

---

# 23. Что важно запомнить после лекции

## Go runtime и goroutines

- Goroutine планирует Go runtime, OS thread планирует ОС.
- G — runtime execution context goroutine.
- Большое количество goroutines возможно благодаря multiplexing и growable stacks.
- Goroutine дешёвая относительно thread, но не бесплатная.

## G / M / P

- G — работа.
- M — OS thread.
- P — runtime resource/right to execute ordinary Go code.
- M для выполнения user Go code нужен P.
- Количество M может быть больше `GOMAXPROCS`.

## Scheduler

- У P есть local runnable work.
- Есть global runnable queue.
- Work stealing перераспределяет load.
- `runnext` снижает latency связанных scheduling events.
- Scheduler пытается одновременно сохранить locality, fairness и низкий thread churn.

## Netpoller и syscalls

- Pollable network wait обычно park'ит G, а не dedicated M.
- Blocking syscall может реально заблокировать M.
- P может быть использован другим M, пока исходный thread ждёт kernel.

## Preemption

- CPU-bound G не должна бесконечно монополизировать P.
- Async preemption улучшает progress, но Go не является real-time scheduler.

## Stack

- Goroutine stack growable и movable.
- Compiler stack maps позволяют runtime корректировать pointers.
- Growth выполняется через system stack `g0`.
- Реальная стоимость G гораздо шире одного initial stack size.

## Parking

- `gopark`: RUNNING → WAITING.
- `goready`: WAITING → RUNNABLE.
- Wakeup не означает мгновенное выполнение.
- Lost wakeup предотвращается правильным waiter registration protocol.

## Channels и `sudog`

- `sudog` представляет G в конкретной wait queue.
- Blocking channel operation может park текущую G.
- Non-blocking operation не создаёт waiting state для текущей G.
- `select` объясняет, зачем одной G может понадобиться несколько sudog.

## Mutex

- Uncontended Mutex старается пройти только через fast atomic path.
- При contention runtime может коротко spin'иться, затем park G.
- Normal mode допускает barging ради throughput.
- Starvation mode повышает fairness ценой части throughput.

## Runtime semaphore

- Semaphore machinery связывает synchronization state с `sudog`, wait queues, `gopark` и `goready`.
- Полноценная wait queue не хранится непосредственно внутри каждого Mutex.

## Atomics и Memory Model

- CAS — atomic read-modify-write и основа многих runtime state machines.
- Lock-free не означает contention-free.
- Data race — ошибка synchronization protocol.
- Channel, Mutex и atomics задают memory ordering guarantees.
- Atomic отдельных fields не делает composite invariant атомарным.

---

# 24. Инженерный вывод

Go позволяет писать backend так, будто каждая concurrent operation имеет свой простой последовательный flow:

```go
func handle(conn net.Conn) {
    n, err := conn.Read(buf)
    if err != nil {
        return
    }

    process(buf[:n])
}
```

Но под этой простотой работает большая runtime machine:

```text
G
↓
P/M scheduler
↓
run queues
↓
work stealing
↓
netpoller
↓
parking/wakeup
↓
stack management
↓
synchronization
↓
atomics + memory ordering
```

Главная инженерная ценность знания internals — не возможность переписать scheduler.

Она в другом: по наблюдаемому симптому понимать, какой слой системы ограничивает backend.

```text
много WAITING G
→ что они ждут?

много RUNNABLE G
→ хватает ли CPU?

threads растут
→ blocking syscall/cgo?

low CPU + high latency
→ lock/pool/network wait?

goroutine count растёт
→ normal concurrency или leak?

Mutex profile горячий
→ насколько сериализован critical path?

100 000 G ждут DB pool из 20 connections
→ scheduler работает нормально,
  но application не имеет backpressure
```

Именно здесь runtime internals возвращаются обратно в production backend: они позволяют отличать проблему scheduler от проблемы synchronization, I/O, CPU capacity и архитектуры resource limiting.

---

---

# 25. Расширенный разбор обязательных механизмов

Ниже основной материал той же лекции раскрыт ещё на один слой глубже: не как отдельная «дополнительная» тема про CPU/MESI, а как подробное объяснение именно обязательных механизмов runtime, которые нужны для понимания поведения Go backend под нагрузкой.

## 25.1. Go runtime как слой между языком и операционной системой

Когда мы пишем:

```go
go handle(conn)
```

язык обещает concurrent execution новой goroutine. Но ни Linux, ни Windows, ни macOS не знают сущность «goroutine». Для операционной системы существуют процессы, threads, virtual memory, file descriptors, timers и syscalls.

Поэтому между исходным кодом и OS существует Go runtime:

```text
Go source
↓
compiler
↓
native machine code
+
Go runtime
↓
OS threads / syscalls / virtual memory
↓
CPU / kernel / hardware
```

Runtime решает задачу мультиплексирования: goroutines обычно существенно больше, чем OS threads.

Представим backend:

```text
50 000 HTTP connections
20 000 goroutines ждут network
5 000 goroutines ждут channels/mutexes
200 goroutines runnable
8 CPU cores
```

ОС не должна получить 25 000 sleeping threads. Runtime умеет убрать ожидающую G из runnable work и продолжить выполнять другие G на ограниченном наборе M.

В этом и находится центральная ценность модели Go:

```text
логическое ожидание операции
≠
обязательное физическое ожидание OS thread
```

Но эта возможность не бесплатна. Runtime должен вести scheduler state, stacks, queues, timers, poll descriptors, synchronization waiters и GC metadata.

---

## 25.2. `go f()` — это создание runnable computation, а не немедленный запуск

Возьмём:

```go
func main() {
    go worker()
    doSomething()
}
```

Плохая mental model:

```text
go worker()
↓
worker немедленно начинает выполняться
```

Точнее:

```text
current G выполняет go statement
↓
runtime подготавливает новую G
↓
новая G получает stack + execution context
↓
G становится RUNNABLE
↓
G помещается в scheduler queue
↓
когда scheduler выберет G
↓
RUNNABLE → RUNNING
↓
worker действительно начинает выполнение
```

Поэтому программа:

```go
go func() {
    fmt.Println("worker")
}()

fmt.Println("main")
```

не имеет гарантированного порядка этих двух `Println` только из-за порядка строк.

Если порядок нужен, его выражают synchronization primitives.

---

## 25.3. Что именно хранит `G`

Не нужно превращать лекцию в чтение `runtime/runtime2.go`, но важно понимать категорию данных.

`G` должна позволять runtime остановить computation и потом продолжить её с того же места.

Упрощённо:

```text
G
├── status
├── stack bounds
├── saved PC/SP и scheduler context
├── pointer на M, если G сейчас RUNNING
├── wait reason / waiting structures
├── goroutine id/runtime bookkeeping
└── GC/scheduler metadata
```

То есть G — это не «адрес функции».

Если goroutine находится здесь:

```go
func handler() {
    user := loadUser()
    order := buildOrder(user)
    result := <-responseCh
    save(result)
}
```

и блокируется на `<-responseCh`, runtime должен сохранить computation так, чтобы позже продолжить именно после receive, сохранив call stack и локальное состояние.

Модель:

```text
RUNNING
↓
park
↓
WAITING 10 секунд
↓
ready
↓
RUNNABLE
↓
scheduler
↓
RUNNING
↓
продолжение после receive
```

---

## 25.4. Состояния G как state machine

Для прикладного понимания полезна такая модель:

```text
              ┌───────────────┐
              │   RUNNABLE    │
              └───────┬───────┘
                      │ scheduler
                      ▼
              ┌───────────────┐
              │    RUNNING    │
              └─┬─────┬─────┬─┘
                │     │     │
          block │     │     │ syscall
                │     │     │
                ▼     │     ▼
            WAITING   │   SYSCALL
                │     │     │
          wake  │     │     │ return
                └──►RUNNABLE◄┘
                      │
                      │ finish
                      ▼
                    DEAD
```

Важно не просто запомнить названия, а понимать различие.

### RUNNABLE

G уже может выполняться, но пока не получила P/M/CPU.

Это потенциальная scheduler latency.

### RUNNING

G прямо сейчас выполняет Go code на M, которому принадлежит P.

### WAITING

Продолжать computation невозможно до события:

- channel;
- mutex/semaphore;
- timer;
- network readiness;
- select;
- другие runtime waits.

### SYSCALL

G/M находятся в syscall path. Это отдельное состояние, потому что OS thread может быть занят kernel operation, а P runtime выгодно передать другому M.

### DEAD

G закончила computation и может быть переиспользована runtime.

---

## 25.5. RUNNABLE и WAITING — принципиально разные причины latency

Это одна из самых полезных диагностических идей лекции.

Пусть request отвечает 3 секунды.

Вариант A:

```text
G WAITING 2.9 s on network
RUNNABLE 0.01 s
RUNNING 0.09 s
```

Проблема ближе к downstream/network.

Вариант B:

```text
G RUNNABLE 2.5 s
RUNNING кусками 0.5 s
```

Здесь работа готова, но CPU/scheduler capacity не хватает.

Обе ситуации выглядят для пользователя одинаково:

```text
HTTP latency = 3 s
```

Но лечатся совершенно по-разному.

---

## 25.6. Почему goroutine дешёвая относительно OS thread

OS thread обычно требует kernel-visible thread state и существенно более тяжёлую thread lifecycle machinery.

Goroutine создаётся и планируется в user-space runtime.

Она использует:

- небольшой growable stack;
- compact runtime descriptor;
- Go scheduler вместо отдельного kernel scheduler entity;
- parking без обязательного sleeping thread.

Поэтому возможно:

```text
100 000 G
```

при десятках M, а не 100 000 M.

Но из этого нельзя делать вывод:

> Goroutine бесплатна.

Цена включает:

```text
stack
+ G metadata
+ scheduler bookkeeping
+ GC scanning
+ captured/retained heap objects
+ wait structures
+ external resources
```

---

## 25.7. Goroutine leak как retention problem

Пример:

```go
func start(ch <-chan string) {
    go func() {
        buf := make([]byte, 8<<20)
        value := <-ch
        _ = value
        _ = buf
    }()
}
```

Если никто никогда не отправит в `ch` и не закроет его:

```text
G → WAITING forever
```

Но одновременно:

```text
G stack остаётся root
↓
stack/reference graph остаётся reachable
↓
8 MiB buffer может оставаться живым
```

Поэтому leak goroutine часто означает не только несколько килобайт runtime overhead.

Она может удерживать:

- request body;
- response;
- transaction;
- connection;
- large buffer;
- domain graph;
- timers.

---

## 25.8. G / M / P: точная учебная модель

Три сущности отвечают на три разных вопроса.

```text
G — что нужно выполнить?
M — какой OS thread сейчас выполняет инструкции?
P — какой runtime execution resource позволяет M выполнять Go code?
```

Упрощённо:

```text
G
↓
M + P
↓
OS scheduler
↓
CPU
```

Важно: ОС планирует M, а Go scheduler планирует G.

Получается два scheduler слоя:

```text
Go scheduler:
G → M/P

OS scheduler:
M → CPU
```

---

## 25.9. Почему нельзя отождествлять P и CPU

Удобно думать:

```text
GOMAXPROCS ≈ число P ≈ разрешённый parallelism Go code
```

Но P не является физическим core.

Если:

```text
GOMAXPROCS = 8
```

runtime имеет восемь execution resources для параллельного Go code.

Но ОС может физически дать процессу меньше CPU time из-за:

- cgroup quota;
- competing processes;
- host load;
- scheduling policy.

То есть:

```text
P = runtime capacity token/state
CPU = hardware
M = OS-scheduled bridge между ними
```

---

## 25.10. Почему P нельзя просто убрать

Вообразим runtime только из:

```text
G + M
```

Тогда local scheduler state, allocator caches и execution resources сильнее привязываются к OS thread.

Теперь M1 делает blocking syscall:

```text
M1 blocked in kernel
```

Если scheduler capacity также привязана к M1, мы временно теряем её.

С P runtime может сделать:

```text
G1/M1/P0
↓
M1 enters syscall
↓
P0 detaches
↓
M2 acquires P0
↓
G2 executes
```

Именно это делает P самостоятельной полезной сущностью.

---

## 25.11. Что живёт на P

Конкретные поля меняются между версиями, но архитектурная идея стабильна: часть hot runtime state выгодно держать per-P.

Например:

```text
P
├── local run queue
├── runnext
├── allocator caches
├── sudog/defer caches
├── timer/runtime bookkeeping
└── scheduler state
```

Это уменьшает global synchronization.

Вместо:

```text
64 P
↓
один global allocator/scheduler lock
```

много fast paths остаются локальными.

---

## 25.12. GOMAXPROCS — не worker pool

Это одна из самых важных границ.

Пусть:

```text
GOMAXPROCS = 8
```

Это не означает:

```text
не более 8 HTTP requests одновременно
```

И не означает:

```text
не более 8 DB requests одновременно
```

Тысячи G могут:

```text
немного выполнить Go code
↓
уйти в network WAITING
```

И одновременно иметь тысячи in-flight external operations.

`GOMAXPROCS` ограничивает parallel execution Go code, а downstream concurrency нужно ограничивать отдельно.

---

## 25.13. CPU-bound и I/O-bound сервисы реагируют на GOMAXPROCS по-разному

### CPU-bound

```text
JSON/crypto/compression/calculation
```

Здесь больше P до разумного hardware limit может дать больше parallelism.

### I/O-bound

```text
HTTP dependency
PostgreSQL
network storage
```

Если 95% времени G ждёт внешнюю систему, ещё четыре P могут почти ничего не изменить.

Поэтому tuning начинается с вопроса:

> Где реально проводится время request?

а не с автоматического изменения `GOMAXPROCS`.

---

## 25.14. Container-aware GOMAXPROCS и throttling

Представим Kubernetes container:

```text
host: 64 CPU
CPU limit: 2
```

Если runtime активно ведёт себя как на 64-way machine:

```text
много parallel CPU work
↓
quota быстро исчерпывается
↓
cgroup throttling
↓
process временно не получает CPU
↓
tail latency растёт
```

Современный Go учитывает container CPU limit при default `GOMAXPROCS` на Linux.

Но это не значит, что runtime решил все capacity problems.

Нужно всё равно смотреть:

- CPU throttling metrics;
- actual workload;
- thread count;
- runnable queue;
- latency distribution.


# 26. Scheduler, run queues и work stealing — подробнее

## 26.1. Scheduler решает не одну задачу

На первый взгляд задача простая:

```text
есть RUNNABLE G
↓
выбрать следующую
```

Но реальный scheduler одновременно пытается сохранить:

- throughput;
- fairness;
- locality;
- low latency;
- небольшое количество активных OS threads;
- progress GC/runtime tasks;
- баланс между P.

Эти цели конфликтуют.

Например:

```text
держать G на том же P
```

полезно для cache locality.

Но:

```text
если соседний P idle
```

лучше отдать ему часть work.

Отсюда work stealing.

---

## 26.2. Почему одна global queue плохо масштабируется

Представим:

```text
P0 ─┐
P1 ─┤
P2 ─┼──► GLOBAL RUN QUEUE
P3 ─┤
... │
P63─┘
```

Каждый P постоянно:

```text
enqueue
↓
dequeue
↓
shared synchronization
```

Одна структура становится hot shared state.

Поэтому Go использует distributed scheduler:

```text
P0.runq
P1.runq
P2.runq
...
+
global run queue
```

Local path дешёвый.

Global structures нужны, когда нужна coordination.

---

## 26.3. Local run queue

Ментальная модель:

```text
P0
├── runnext → G17
└── runq:
    G21
    G22
    G23
```

Если P уже выполняет связанный workload, держать новые G локально выгодно:

```text
меньше cross-P synchronization
+
лучше locality
```

Но local queue конечна.

При overflow часть work уходит в global scheduling machinery.

Конкретный размер очереди — implementation detail текущего runtime, а не public contract.

---

## 26.4. `runnext`

`runnext` — отдельный slot G, которой дают повышенный шанс выполниться следующей на этом P.

Сценарий:

```text
G1 выполняется
↓
G1 создаёт/разблокирует G2
↓
G2 → runnext
↓
после G1 scheduler предпочитает G2
```

Это особенно полезно для producer-consumer chain.

Почему:

```text
latency меньше
cache locality потенциально лучше
```

Но `runnext` не создаёт вечную parent-child связь между G.

«Связанная goroutine» здесь — удобная causal mental model, а не постоянное поле runtime.

---

## 26.5. `inheritTime`

G из `runnext` может продолжить текущую scheduler slice.

Упрощённо:

```text
G1
↓
G2 via runnext
↓
G3 via runnext
```

могут идти в рамках одного внутреннего scheduler tick/accounting interval.

Это уменьшает latency handoff.

Но если бесконечно продолжать такую цепочку:

```text
G1 → G2 → G3 → G4 → ...
```

остальные G могут страдать.

Поэтому fairness и preemption остаются необходимы.

---

## 26.6. `schedtick`

Важно не объяснять `schedtick` как аппаратный timer tick.

Это внутренний scheduler progress counter P.

Упрощённо:

```text
новая scheduler slice
↓
schedtick++
```

Если G запускается с `inheritTime`, отдельный новый tick может не начинаться.

Runtime может смотреть, как долго P остаётся без scheduler progress.

Это помогает обнаруживать слишком длинную execution slice.

---

## 26.7. `schedule()` и `findRunnable()`

Когда M должен найти следующую G:

```text
schedule()
↓
findRunnable()
↓
execute(G)
```

`findRunnable` не читает одну FIFO queue.

Он рассматривает несколько источников work:

```text
runtime/GC tasks
local runq
global runq
timers
netpoller
work stealing
```

Точный порядок проверок может меняться между версиями.

Нужная студенту модель:

> Scheduler ищет самый разумный доступный источник runnable work, постепенно расширяя область поиска.

---

## 26.8. Fairness против locality

Если всегда выбирать local queue:

```text
P0 local work never ends
↓
global work waits
```

Плохая fairness.

Если постоянно брать global work:

```text
shared synchronization ↑
locality ↓
```

Плохой throughput.

Поэтому scheduler периодически делает fairness checks.

Это классический tradeoff:

```text
локальная эффективность
vs
глобальная справедливость
```

---

## 26.9. Work stealing

Пусть:

```text
P0 empty
P1 empty
P2: G1 G2 G3 G4 G5 G6
P3 empty
```

Если ничего не делать:

```text
P2 busy
P0/P1/P3 idle
```

Scheduler позволяет idle P украсть часть run queue victim P.

```text
P0
↓
choose victim P2
↓
steal batch
↓
P0 executes stolen work
```

Это dynamic load balancing.

---

## 26.10. Почему steal batch

Если воровать по одной G:

```text
steal
run
steal
run
steal
```

coordination overhead высок.

Batch позволяет:

```text
одна cross-P operation
↓
несколько последующих local executions
```

То есть стоимость stealing амортизируется.

---

## 26.11. Почему work stealing может ухудшить locality

G могла до этого выполняться рядом с данными, горячими в cache одного core.

После steal:

```text
G → другой P/M/core
```

новый core может получить cache misses.

Поэтому scheduler постоянно балансирует:

```text
использовать idle CPU
vs
сохранить cache locality
```

Это особенно важно на больших CPU/NUMA machines.

---

## 26.12. Spinning M

Когда work сейчас не найдено, есть два крайних решения.

### Сразу park

```text
нет work
↓
thread sleep
```

Если G появится через мгновение, thread нужно снова wake.

### Бесконечно искать

```text
нет work
↓
scan forever
```

CPU тратится впустую.

Go использует промежуточную стратегию:

```text
spinning M
```

M некоторое время активно ищет work.

Если work появляется, latency низкая.

Если нет — M park'ится.

---

## 26.13. Почему нельзя иметь много spinning M

Пусть 64 P и work отсутствует.

Если все M spin:

```text
64 CPU busy
полезная работа = 0
```

Поэтому runtime ограничивает число spinning workers.

Это energy/CPU efficiency tradeoff.

---

## 26.14. `wakep()`

Когда появляется runnable G, runtime решает:

> Нужно ли активировать ещё один worker?

Если будить thread на каждую новую G:

```text
wake
↓
thread обнаружил, что работу уже забрали
↓
park
```

получим thread thrashing.

Поэтому wake policy консервативна.

Идея:

```text
idle P есть
+
нет уже spinning worker, который найдёт work
↓
имеет смысл wake дополнительный M
```

---

## 26.15. Scheduler latency

Очень полезная метрика mental model:

```text
time G became RUNNABLE
↓
time G became RUNNING
```

Если CPU свободен, она мала.

Если CPU saturated:

```text
run queues grow
↓
RUNNABLE wait grows
↓
request p99 grows
```

Это latency без network wait и без mutex wait.

---

## 26.16. Почему scheduler не является backpressure

Пусть endpoint создаёт goroutine на каждую задачу:

```go
for _, job := range jobs {
    go process(job)
}
```

Scheduler корректно поставит миллионы G в states/queues.

Но если CPU способен завершать:

```text
10 000 jobs/s
```

а producer создаёт:

```text
100 000 jobs/s
```

очередь будет расти.

Runtime не знает, что вы хотели ограничить latency.

Нужен application-level bounded queue/admission control.

---

# 27. Netpoller, syscalls и preemption — подробнее

## 27.1. Почему blocking-style network API не означает blocking thread

Код:

```go
n, err := conn.Read(buf)
```

выглядит синхронным.

Если data нет, function действительно не может продолжить computation.

Но runtime может сделать:

```text
Read would block
↓
зарегистрировать fd в netpoller
↓
park G
↓
M/P выполняют другую G
```

То есть:

```text
blocking API semantics
```

сохраняются для programmer, но runtime implementation использует readiness-based I/O.

---

## 27.2. Readiness model

Kernel polling API сообщает примерно:

```text
этот fd теперь имеет шанс выполнить read/write без обычного blocking
```

а не:

```text
весь логический HTTP request полностью готов
```

Поэтому lifecycle может быть:

```text
RUNNING
↓
Read gets some bytes
↓
would block again
↓
WAITING
↓
readiness event
↓
RUNNABLE
↓
RUNNING
```

многократно.

---

## 27.3. `pollDesc`

У pollable descriptor runtime хранит структуру состояния.

Для mental model:

```text
fd
↓
pollDesc
├── reader wait state
├── writer wait state
├── read deadline
└── write deadline
```

Она связывает kernel fd с waiting goroutines.

---

## 27.4. Что может разбудить network waiter

Не только incoming packet.

Возможные события:

```text
fd readable/writable
connection closed
deadline expired
runtime cancellation-related path
```

После этого:

```text
WAITING → RUNNABLE
```

а operation продолжает execution и возвращает соответствующий результат/error.

---

## 27.5. Почему timeout не создаёт отдельный sleeping thread

Deadline интегрирован с runtime timer/poll machinery.

Поэтому 100 000 network deadlines не означают:

```text
100 000 OS threads sleeping with timers
```

Это ещё один пример multiplexing runtime state поверх ограниченных kernel resources.

---

## 27.6. Blocking syscall принципиально другой

Не всякая I/O operation может быть представлена pollable readiness wait.

Если thread реально вошёл в blocking syscall:

```text
M blocked by kernel
```

он физически не может выполнять другую G.

Но P можно освободить/retake и передать другому M.

```text
G1/M1/P0
↓
syscall
↓
M1 blocked
↓
P0 → M2
↓
G2 runs
```

---

## 27.7. Почему OS thread count может быть больше GOMAXPROCS

Потому что часть M может одновременно:

- находиться в syscall;
- выполнять cgo;
- быть в runtime transitions;
- быть parked/idle.

`GOMAXPROCS` ограничивает число P, а не lifetime всех OS threads runtime.

---

## 27.8. cgo и foreign code

В foreign function runtime не контролирует внутренний blocking так же хорошо, как собственный netpoll path.

Поэтому много long-running cgo calls может привести к:

```text
more blocked M
↓
more M needed for runnable Go work
↓
thread count grows
```

Это важный production diagnostic.

---

## 27.9. Preemption: зачем она вообще нужна

Parking работает только если G сама доходит до operation, которая может её остановить.

CPU-bound code:

```go
for {
    compute()
}
```

может долго не ждать ничего.

Без preemption такая G потенциально удерживает P слишком долго.

---

## 27.10. Cooperative и asynchronous preemption

Исторически runtime сильнее зависел от cooperative safe points:

```text
function calls
stack checks
runtime calls
```

Современный Go умеет инициировать asynchronous preemption на поддерживаемых platform paths.

Но это не означает:

```text
остановить G в любой случайной machine instruction без ограничений
```

Runtime всё равно должен иметь безопасный state для stack/GC.

---

## 27.11. Почему preemption нужна GC

GC должен получать progress и иметь возможность корректно анализировать goroutine roots/stacks.

Если G бесконечно монополизирует execution:

```text
scheduler progress плохой
+
GC coordination плохая
```

Поэтому async preemption улучшает не только fairness application G, но и runtime progress.

---

## 27.12. Preemption не является cancellation

Context отменился:

```text
ctx.Done()
```

Runtime scheduler не обязан автоматически завершить вашу CPU function.

Preemption означает:

```text
дать CPU другим G
```

Cancellation означает:

```text
application должна перестать делать ненужную работу
```

Это разные задачи.

---

## 27.13. Preemption не решает overload

Пусть CPU может обработать:

```text
1 000 requests/s
```

а приходит:

```text
10 000 requests/s
```

Preemption может честно делить CPU между requests.

Но суммарная capacity всё равно 1 000 requests/s.

Получаем:

```text
runnable backlog ↑
latency ↑
timeouts ↑
```

Нужен admission control.


# 28. Stack growth и реальная цена goroutine — подробнее

## 28.1. Stack растёт из-за call frames, а не из-за времени жизни

Пусть goroutine живёт час, но call depth небольшой.

Её stack может оставаться маленьким.

Другая goroutine живёт 10 ms, но уходит в глубокую recursion и получает большой stack.

То есть stack pressure определяется прежде всего:

- глубиной вызовов;
- размером frames;
- compiler decisions;
- редкими deep paths.

Модель:

```text
handler
↓
service
↓
validation
↓
parser
↓
recursive traversal
↓
ещё frames
```

---

## 28.2. Что находится в stack frame

Упрощённо frame может содержать:

- local variables;
- spill slots;
- return state;
- arguments/results согласно calling convention;
- temporary compiler values.

Поэтому два вызова одинаковой глубины могут требовать разный stack footprint.

---

## 28.3. Почему фиксированный большой stack неудобен для goroutine model

Если каждому execution context сразу дать большой stack:

```text
1 MiB × 100 000 G
```

получаем огромный memory footprint.

Большинство G в backend большую часть времени не используют такую глубину.

Go предпочитает:

```text
small initial stack
↓
grow when needed
```

Это переносит цену из постоянного reservation в редкие growth operations.

---

## 28.4. Stack check

Compiler добавляет проверку в начало большинства функций.

Ментальная модель:

```text
function entry
↓
хватит ли stack для frame?
├── да → continue
└── нет → morestack
```

Runtime использует guard, чтобы не ждать фактического выхода SP за границу memory.

Нужно заранее оставить достаточно пространства для самого growth protocol.

---

## 28.5. Почему growth нельзя делать на перемещаемом stack

Runtime собирается:

```text
allocate new stack
copy old active stack
update pointers
replace bounds
```

Если machinery выполняется на старом stack:

```text
мы пытаемся переносить память,
на которой сами сейчас стоим
```

Поэтому execution переключается на system stack `g0` текущего M.

```text
M
├── current G stack ← объект relocation
└── g0 stack        ← runtime выполняет relocation
```

---

## 28.6. Геометрический рост

Stack увеличивается не на несколько bytes.

Используется multiplicative growth idea:

```text
2
4
8
16
32
...
```

Конкретные размеры/heuristics — implementation detail.

Почему геометрически:

```text
много маленьких growth
→ много copies
```

против:

```text
редкие более крупные growth
→ amortized cost
```

---

## 28.7. Почему `memcpy` недостаточно

Stack содержит managed pointers.

Пример:

```go
func f() {
    x := 10
    p := &x
    use(p)
}
```

Если `x` физически находится в stack:

```text
old stack: 0x100000...
new stack: 0x500000...
```

после простого byte copy `p` может всё ещё содержать старый numeric address.

Runtime должен исправить managed references, которые указывают внутрь перемещённого stack.

---

## 28.8. Stack maps

Откуда runtime знает, что конкретное machine word является pointer?

Не по внешнему виду числа.

Compiler генерирует precise pointer metadata.

Упрощённо:

```text
frame layout
├── slot 0: scalar
├── slot 1: pointer
├── slot 2: scalar
└── slot 3: pointer
```

Эти данные нужны сразу нескольким механизмам:

```text
GC
+
stack copying
```

Это хороший пример тесной связки compiler и runtime.

---

## 28.9. Почему `unsafe`/`uintptr` требуют осторожности

Managed pointer известен runtime как pointer.

`uintptr` — integer representation адреса с особыми правилами.

Если programmer превращает pointer в integer и хранит его произвольно, runtime не обязан рассматривать это число как relocatable managed pointer.

Отсюда строгие правила вокруг `unsafe.Pointer` и `uintptr`.

Именно movable stack делает такие правила особенно понятными.

---

## 28.10. Почему syscall и cgo усложняют relocation

Если stack address передан наружу:

```text
Go stack pointer
↓
kernel/C code
```

runtime не может свободно переместить stack, пока внешняя сторона потенциально использует старый address.

Поэтому stack copying разрешён только в runtime-safe states.

---

## 28.11. `sudog.elem` и stack pointer

Blocked channel operation может иметь waiter metadata, указывающую на element location в stack G.

```text
sudog.elem
↓
G stack
```

Если stack перемещается, runtime должен учесть этот reference.

Это показывает, почему stack growth нельзя рассматривать изолированно от synchronization runtime.

---

## 28.12. Stack shrink

Пусть G однажды выросла:

```text
8 KiB → 256 KiB
```

затем deep call chain закончился, и используется только малая часть.

Если G долгоживущая, хранить peak forever дорого.

Runtime может shrink stack в безопасный момент.

Разница:

```text
grow — срочно, иначе function не продолжит
shrink — optimization, можно выбрать удобный момент
```

---

## 28.13. Source local не означает stack allocation

Пример:

```go
func create() *User {
    u := User{}
    return &u
}
```

`u` написан как local variable, но его lifetime переживает function return.

Compiler решает placement через escape analysis.

Поэтому нельзя оценивать stack usage только по синтаксису исходника.

---

## 28.14. «Stack быстрее heap» — слишком слабая модель

Stack allocation обычно очень дешёвая.

Но production cost включает:

```text
stack growth
copying
pointer adjustment
GC root scanning
cache behaviour
heap escapes
object lifetime
```

Правильный вопрос:

> Каков lifetime и ownership данных и сколько runtime work создаёт выбранная модель?

---

## 28.15. Средний stack важнее минимального при capacity planning

Представим:

```text
100 000 G
```

Если студент считает:

```text
100 000 × 2 KiB
```

он получает лишь грубую нижнюю историческую оценку.

Если реальный средний stack стал:

```text
16 KiB
```

масштаб уже совсем другой.

А ещё G удерживает heap graph.

Поэтому capacity model должна опираться на measurement, а не лозунг «goroutine весит 2 KB».

---

# 29. Parking, wakeup и lost wakeup — подробнее

## 29.1. Центральный механизм дешёвого ожидания

Пусть G не может продолжать:

```text
channel empty
mutex busy
network not ready
timer not fired
```

Runtime должен сделать:

```text
RUNNING
↓
WAITING
```

и вернуть execution resource другим G.

Это и есть фундаментальная разница между:

```text
логически ждать
```

и:

```text
физически занимать OS thread
```

---

## 29.2. Что делает `gopark`

Упрощённо:

```text
current G RUNNING
↓
gopark
↓
save wait metadata
↓
mcall(park_m)
↓
switch to g0
↓
RUNNING → WAITING
↓
dropg
↓
schedule next G
```

То есть `gopark` — вход в protocol parking текущей G.

---

## 29.3. Почему нужен `g0`

После того как G переводится в WAITING, runtime не должен продолжать scheduler machinery на её user stack как будто это обычный user code.

Поэтому transition выполняется на system stack M.

```text
user G stack
↓
M.g0
↓
park_m / scheduler
```

---

## 29.4. `dropg`

До parking:

```text
M.curg → G
G.m → M
```

После:

```text
M больше не исполняет эту G
```

association снимается.

Теперь M/P могут получить другую RUNNABLE G.

---

## 29.5. `goready`

Когда событие произошло:

```text
WAITING
↓
goready/ready
↓
RUNNABLE
```

Ключевой момент:

> Ready — это разрешение выполняться, а не мгновенное выполнение.

После ready G всё ещё конкурирует за scheduler resources.

---

## 29.6. Wakeup latency

Событие произошло в момент `t0`.

G реально продолжилась в `t1`.

Между ними:

```text
ready bookkeeping
run queue
scheduler selection
M availability
OS scheduling M
```

Поэтому:

```text
resource became available
```

не равно:

```text
waiter immediately runs
```

Это критично для понимания Mutex barging.

---

## 29.7. `Gosched` и parking

`Gosched()` означает примерно:

```text
я всё ещё могу продолжать
но добровольно отдам execution другим
```

State:

```text
RUNNING → RUNNABLE
```

Parking:

```text
я не могу продолжать без события
```

State:

```text
RUNNING → WAITING
```

Разница фундаментальная.

---

## 29.8. Preemption и parking

Preemption:

```text
G могла продолжать
но scheduler временно отнял execution
```

обычно снова ведёт к runnable scheduling state.

Parking:

```text
G логически не готова продолжать
```

и должна ждать external/runtime event.

---

## 29.9. Lost wakeup

Наивный protocol:

```text
G1                    G2

check condition
false
unlock

                      event happens
                      wake G1

park
```

Wake произошёл, когда G1 ещё не зарегистрирована как waiter.

Затем G1 park'ится.

Нового event может не быть.

Получаем permanent sleep.

---

## 29.10. Почему waiter должен быть зарегистрирован до sleep

Правильная идея:

```text
lock
↓
check condition
↓
register waiter
↓
prepare parking
↓
release lock coordinated with park
↓
WAITING
```

Теперь producer/event side либо увидит waiter, либо waiter успеет заметить изменившееся condition через recheck protocol.

---

## 29.11. `goparkunlock`

Runtime часто использует variant parking, связанный с release internal lock.

Смысл:

```text
не делать отдельно:
unlock
...
park
```

с опасным race window.

Вместо этого unlock встроен в parking protocol.

---

## 29.12. Wait reason

WAITING G имеет reason, который помогает diagnostics.

Можно увидеть:

```text
[chan receive]
[chan send]
[semacquire]
[select]
[IO wait]
[sleep]
```

Это позволяет быстро классифицировать накопившиеся goroutines.

Но один reason недостаточен: нужен stack trace и понимание lifecycle.

---

## 29.13. Почему много WAITING G может быть нормой

Network server может иметь тысячи idle keep-alive connections.

Если G park'ятся через netpoller:

```text
CPU почти не используется
```

и число G может быть большим, но стабильным.

Проблема начинается, когда:

```text
G count растёт без возврата к baseline
```

или wait reason указывает на condition, которое никогда не наступит.

---

# 30. Channels, `sudog` и wait queues — подробнее

## 30.1. Channel — data structure + synchronization primitive

Плохое упрощение:

> Channel — очередь.

Buffered channel действительно имеет buffer.

Но channel также содержит synchronization state:

```text
buffer
send wait queue
receive wait queue
closed state
internal lock
```

Unbuffered channel вообще не имеет data queue capacity.

---

## 30.2. Ментальная модель `hchan`

```text
hchan
├── qcount      сколько элементов сейчас в buffer
├── dataqsiz    capacity
├── buf         storage
├── sendx       позиция записи
├── recvx       позиция чтения
├── sendq       blocked senders
├── recvq       blocked receivers
├── closed
└── lock
```

Конкретный layout — implementation detail.

Архитектурная идея — нет.

---

## 30.3. Что такое `sudog`

`sudog` — waiter object, связывающий конкретную G с конкретным synchronization wait.

Упрощённо:

```text
sudog
├── g → waiting goroutine
├── elem → data/address для operation
├── next/prev
└── wait metadata
```

Почему нельзя просто хранить `*g`?

Потому что relation many-to-many.

---

## 30.4. Many-to-many

Один channel может иметь много waiters:

```text
ch.recvq
├── sudog(G1)
├── sudog(G2)
└── sudog(G3)
```

Одна G при `select` может быть представлена сразу несколькими sudog:

```text
G42
├── sg1 → ch1.recvq
├── sg2 → ch2.sendq
└── sg3 → ch3.recvq
```

Это и объясняет отдельную structure.

---

## 30.5. Sudog pool

Blocking synchronization очень частая операция.

Если каждый waiter создавать обычной heap allocation:

```text
contention
↓
heap allocations ↑
↓
GC pressure ↑
```

Runtime переиспользует `sudog` через caches/pools.

Модель:

```text
acquireSudog
↓
use in wait queue
↓
releaseSudog
↓
cache
```

---

## 30.6. Unbuffered send, receiver уже ждёт

```go
ch <- x
```

В `recvq` уже есть waiter.

Runtime может:

```text
dequeue receiver sudog
↓
transfer x
↓
ready receiver
↓
sender продолжает
```

Sender сам park'иться не обязан.

---

## 30.7. Unbuffered send, receiver нет

```text
send cannot complete
↓
acquire sudog
↓
sg.g = sender
sg.elem = value location
↓
sendq.enqueue
↓
gopark
↓
sender WAITING
```

Позже receiver завершит rendezvous и разбудит sender.

---

## 30.8. Unbuffered channel как rendezvous

У него нет места для автономного хранения элемента.

Следовательно, successful send означает, что send и receive synchronization пересеклись.

Это важно для memory ordering semantics channel.

---

## 30.9. Buffered send

Если есть место:

```text
qcount < dataqsiz
↓
copy element into buffer
↓
advance send index
↓
return
```

Никакого parking.

Если buffer full:

```text
blocking send
↓
sudog
↓
sendq
↓
park
```

---

## 30.10. Buffered channel не создаёт throughput

Пусть producer:

```text
10 000 msg/s
```

consumer:

```text
1 000 msg/s
```

Buffer 100:

```text
fills quickly
```

Buffer 1 000 000:

```text
fills later
```

Но mismatch остаётся.

Большой buffer меняет:

- время до saturation;
- memory usage;
- queueing latency.

Он не увеличивает consumer service rate.

---

## 30.11. Non-blocking channel operation

```go
select {
case ch <- x:
    // sent
default:
    // not ready
}
```

Если operation сейчас невозможна:

```text
текущая G не создаёт waiter
не gopark
идёт default
```

Но она может использовать sudog другой стороны, если opposite waiter уже существует.

То есть:

> Non-blocking значит «не блокировать текущую G», а не «runtime вообще не касается sudog».

---

## 30.12. `select`

Без `default` и без ready cases:

```text
one G
↓
multiple sudog
↓
register in multiple channel queues
↓
park
```

После события:

```text
one case wins
↓
G ready
↓
losing registrations removed
```

Здесь `sudog` необходим архитектурно.

---

## 30.13. Internal channel lock

Channel имеет internal synchronization, но G не может уснуть, оставив internal lock навечно захваченным.

Нужен protocol:

```text
lock channel
↓
check buffer/waiters/closed
↓
register sudog if needed
↓
park with coordinated unlock
```

Иначе opposite side не сможет прийти и изменить state.

---

## 30.14. `close(ch)`

Close меняет state channel и должен обработать waiters.

После close:

- receivers должны получить возможность завершить operations;
- buffered values сначала остаются читаемыми;
- после drain receive возвращает zero value и `ok=false`;
- send в closed channel приводит к panic.

Close — это synchronization event, а не просто boolean assignment.

---

## 30.15. Channel lifecycle и goroutine leak

Типичный leak:

```go
go func() {
    resultCh <- result
}()
```

а consumer ушёл по timeout и больше channel не читает.

Если channel unbuffered:

```text
sender waits forever
```

Получаем goroutine leak.

Поэтому channel design нужно связывать с cancellation/lifecycle.

---

## 30.16. Когда channel естественен

Channel хорошо выражает:

```text
ownership transfer
event stream
pipeline
bounded queue
worker coordination
```

Mutex естественнее, если нужно:

```text
коротко защитить shared state
```

Механизмы пересекаются, но не являются взаимозаменяемыми лозунгами.


# 31. `sync.Mutex`, runtime semaphore и contention — подробнее

## 31.1. Главная цель Mutex

Mutex защищает invariant shared state:

```text
только один owner critical section одновременно
```

Но runtime implementation должна дополнительно решить:

- как сделать uncontended case дешёвым;
- что делать при кратком contention;
- когда park goroutine;
- кого wake;
- как не разбудить толпу;
- как избежать starvation.

Поэтому внутренний Mutex значительно интереснее идеи «locked/unlocked».

---

## 31.2. Fast path

Свободный mutex:

```text
state = 0
```

Lock пытается сделать atomic transition:

```text
0 → mutexLocked
```

Если CAS успешен:

```text
Lock complete
```

Без:

- sudog;
- runtime semaphore;
- gopark;
- scheduler transition.

Именно поэтому uncontended Mutex очень дешёвый.

---

## 31.3. Почему fast path должен быть минимальным

В нормальной хорошо спроектированной программе многие locks большую часть времени uncontended.

Если даже uncontended `Lock` требовал:

```text
queue lock
scheduler call
allocation
kernel primitive
```

цена synchronization была бы высокой всегда.

Go выносит сложность в slow path.

---

## 31.4. Failed CAS не означает немедленный sleep

Пусть owner скоро освободит lock.

Если contender сразу park'ится:

```text
enqueue
park
wake
scheduler
```

стоимость может быть выше, чем очень короткий active wait.

Поэтому runtime иногда spin'ится.

---

## 31.5. Spinning

Spin означает:

```text
G остаётся RUNNING
M/P/CPU заняты
G несколько раз проверяет возможность progress
```

Это выгодно только если ожидание действительно короткое.

Цена spin:

```text
CPU consumption
+
shared cache-line pressure
```

Поэтому он bounded и heuristic-based.

---

## 31.6. Почему spin бессмыслен при отсутствии parallelism

При `GOMAXPROCS=1` contender spin'ится на единственном P.

Но owner lock может быть другой runnable G, которой как раз нужен этот P, чтобы сделать Unlock.

Получаем абсурд:

```text
waiter busy-spins
↓
owner не получает execution
↓
lock не освобождается
```

Runtime учитывает scheduler conditions и не spin'ится бездумно.

---

## 31.7. Parking при тяжёлом contention

Если ожидание явно не краткое:

```text
slow path
↓
runtime semaphore
↓
register waiter
↓
sudog
↓
gopark
↓
WAITING
```

Теперь waiter перестаёт потреблять CPU.

M/P могут выполнять другую работу.

---

## 31.8. `Mutex.state`

Текущая implementation кодирует несколько components в одном `int32`:

```text
31                                3 2 1 0
┌──────────────────────────────────┬─┬─┬─┐
│          waiter count            │S│W│L│
└──────────────────────────────────┴─┴─┴─┘
```

Где:

```text
L = locked
W = woken coordination bit
S = starvation mode
```

Смысл packed word:

```text
несколько связанных state transitions
↓
один atomic CAS
```

---

## 31.9. Waiter count

Когда G понимает, что ей придётся ждать, protocol должен отразить waiter до фактического sleep.

Почему:

```text
Unlock должен увидеть,
что кого-то нужно wake
```

Если сначала park, потом пытаться register waiter, wakeup можно потерять.

---

## 31.10. `mutexWoken`

Пусть несколько G sleeping.

Unlock будит одного waiter.

Без coordination другой concurrent path может решить:

```text
waiters есть
→ wake ещё одного
```

и вызвать thundering herd.

`mutexWoken` означает примерно:

> Уже существует активированный contender; лишний wake сейчас не нужен.

Это не ownership bit.

---

## 31.11. Thundering herd

Наивный Unlock:

```text
wake all waiters
```

Если 1000 G waiting:

```text
1000 WAITING → RUNNABLE
↓
1000 scheduler entries
↓
1000 contenders на один lock
↓
1 winner
999 losers
```

Получаем огромную бессмысленную работу.

Правильная стратегия старается активировать ограниченное число contenders.

---

## 31.12. Normal mode и barging

В normal mode wake waiter не означает direct ownership.

Timeline:

```text
G1 owns lock
G2 WAITING

G1 Unlock
↓
G2 RUNNABLE

G3 already RUNNING
↓
G3 Lock
↓
G3 wins CAS

G2 later RUNNING
↓
lock busy again
```

G3 barged ahead of older waiter.

Почему это допускается:

```text
G3 уже на CPU
↓
дать ей lock может быть дешевле,
чем ждать schedule G2
```

Это throughput optimization.

---

## 31.13. Цена barging

Если поток newcomers непрерывен:

```text
старый waiter wakes
↓
loses
↓
sleeps
↓
wakes
↓
loses
```

Tail latency отдельной G может стать очень плохой.

Тогда throughput policy начинает нарушать fairness.

---

## 31.14. Starvation mode

После достаточно долгого wait runtime может переключить Mutex в starvation protocol.

Идея:

```text
старый waiter должен получить progress
```

В этом режиме newcomers не должны steal ownership так же свободно.

Unlock делает более прямой handoff waiter.

Цена:

```text
fairness ↑
throughput может ↓
```

Конкретный временной threshold текущей implementation — detail, а не public guarantee.

---

## 31.15. Почему starvation mode не включён всегда

Если всегда делать строгий FIFO/direct handoff:

```text
Unlock
↓
wake old waiter
↓
ждать его scheduling
```

можно терять throughput на коротких critical sections.

Normal mode оптимизирован под common fast contention.

Starvation mode — механизм восстановления fairness при плохом сценарии.

---

## 31.16. Critical section и I/O

Опасный код:

```go
mu.Lock()
defer mu.Unlock()

state.Value++
resp, err := client.Do(req)
```

Если внешний request идёт 3 секунды:

```text
lock held 3 seconds
```

Все остальные users shared state стоят в очереди.

Возможный результат:

```text
CPU low
throughput low
hundreds G WAITING [semacquire]
```

Симптом выглядит не как CPU overload, но сервис практически сериализован.

---

## 31.17. Runtime semaphore

Поле `sema` у Mutex используется внутренней runtime machinery для sleep/wakeup.

Важно разделять:

```text
Mutex.state
→ logical ownership/fairness protocol

runtime semaphore
→ waiter parking/wakeup machinery
```

Semaphore не является самой логикой Mutex ownership.

---

## 31.18. Почему это не просто kernel semaphore

Если каждый contended lock немедленно делал отдельный kernel synchronization call, runtime терял бы часть преимуществ user-space scheduling.

Go runtime организует waiters как G/sudog и park'ит goroutine.

Kernel thread при этом часто остаётся доступен Go scheduler.

---

## 31.19. `semaRoot`

Mutex не содержит полноценную queue object.

Это держит Mutex маленьким.

Runtime имеет shared semaphore wait structures, выбираемые по адресу semaphore.

Mental model:

```text
&m.sema
↓
semaphore table/hash mapping
↓
semaRoot
↓
waiter structures
↓
sudog
↓
G
```

Конкретная структура таблицы — implementation detail.

---

## 31.20. Recheck против lost wakeup

Acquire slow path должен учитывать race:

```text
G1 checks resource unavailable
↓
G2 releases resource
↓
G1 decides to sleep
```

Если G1 не перепроверит состояние после регистрации waiter, release может потеряться.

Поэтому semaphore protocol имеет waiter accounting + recheck.

Это тот же общий invariant:

> Event и registration waiter должны быть согласованы так, чтобы wakeup нельзя было потерять.

---

## 31.21. Low CPU не исключает contention

Сценарий:

```text
1 G RUNNING inside lock
999 G WAITING on lock
```

CPU может быть 10–20%.

Но throughput ограничен одним critical section.

Поэтому contention ищут:

- mutex profile;
- block profile;
- goroutine stacks;
- trace.

Не по CPU graph alone.

---

## 31.22. Mutex profile

Профиль помогает ответить:

```text
Где приложение теряет время на lock contention?
```

Но профиль не говорит автоматически, как исправить architecture.

Нужно понять:

- lock scope;
- protected invariant;
- есть ли I/O внутри;
- можно ли sharding;
- нужен ли вообще shared mutable state.

---

# 32. Atomics, CAS и Go Memory Model — подробнее

## 32.1. `atomic` отвечает не на один вопрос

Когда разработчик говорит:

> Сделаем поле атомарным.

нужно спросить:

```text
Какой invariant мы защищаем?
```

Atomicity одной variable не гарантирует correctness всей state machine.

---

## 32.2. CAS

Compare-And-Swap концептуально:

```text
if *addr == expected {
    *addr = desired
    return true
}
return false
```

но проверка и запись происходят атомарно относительно других atomic participants.

---

## 32.3. CAS loop

Типичный optimistic pattern:

```go
for {
    old := state.Load()
    new := compute(old)

    if state.CompareAndSwap(old, new) {
        break
    }
}
```

Failure означает:

```text
кто-то изменил state после нашего Load
```

Нужно reread и recompute.

---

## 32.4. CAS как основа packed state machine

Mutex state — хороший пример.

Нужно атомарно изменить несколько logical components:

```text
Locked
Woken
Starving
waiter count
```

Они упакованы в один word.

Тогда transition:

```text
old bits
↓
compute new bits
↓
CAS
```

сохраняет связность invariant.

---

## 32.5. Atomicity, visibility и ordering

Это три разных вопроса.

### Atomicity

Операция не наблюдается наполовину.

### Visibility

Когда другой participant гарантированно увидит write?

### Ordering

В каком порядке memory effects должны наблюдаться относительно synchronization event?

Memory Model описывает именно эти отношения на language level.

---

## 32.6. Неправильная публикация

```go
var ready bool
var data int

func writer() {
    data = 42
    ready = true
}

func reader() {
    for !ready {
    }
    fmt.Println(data)
}
```

Интуитивное рассуждение:

```text
data записан раньше ready
```

недостаточно.

Здесь data race.

Без synchronization program не получает необходимых visibility/ordering guarantees.

---

## 32.7. Публикация через channel

```go
var data int
ready := make(chan struct{})

go func() {
    data = 42
    close(ready)
}()

<-ready
fmt.Println(data)
```

Synchronization event связывает write и subsequent read.

Mental model:

```text
write data
↓
channel synchronization
↓
reader observes synchronization
↓
read data
```

---

## 32.8. Публикация через Mutex

Writer:

```go
mu.Lock()
data = 42
mu.Unlock()
```

Reader:

```go
mu.Lock()
v := data
mu.Unlock()
```

Mutex даёт не только mutual exclusion, но и memory ordering guarantee.

Это принципиально важно.

---

## 32.9. Goroutine creation и ordering

Создание goroutine само по себе имеет определённые memory-order semantics для действий до `go` statement относительно старта новой G.

Но обратного автоматического ordering:

```text
новая G что-то записала
↓
parent потом обязательно увидит без synchronization
```

нет.

Чтобы дождаться результата, нужен synchronization mechanism.

---

## 32.10. Atomic field не защищает composite invariant

```go
type State struct {
    balance atomic.Int64
    status  atomic.Int32
}
```

Допустим invariant:

```text
status=CLOSED ⇒ balance=0
```

Отдельно atomic `balance` и `status` не обеспечивают atomic transition пары.

Reader может увидеть промежуточную комбинацию.

Если нужен composite invariant, часто проще Mutex или single packed atomic state.

---

## 32.11. Public Go atomics и ordering

Программист должен рассуждать через Go Memory Model, а не через конкретные CPU instructions.

Implementation может использовать разные sequences на:

- x86-64;
- ARM64;
- других architectures.

Публичный contract остаётся Go-level.

---

## 32.12. Почему atomic operation всё равно может быть дорогой

Пусть:

```go
counter.Add(1)
```

делают десятки cores.

Логически это lock-free operation.

Физически все пишут одну cache line.

Hardware должен постоянно согласовывать ownership.

Получаем:

```text
atomic
≠
free
≠
contention-free
```

---

## 32.13. Failed CAS под нагрузкой

32 contenders:

```text
Load old
↓
compute new
↓
CAS
```

один winner.

31 делают retry.

Если contention постоянен, значительная часть CPU уходит в coordination, а не business work.

---

## 32.14. Почему Mutex иногда лучше hot CAS loop

При тяжёлом contention Mutex может park лишних contenders.

Hot lock-free CAS loop может держать все cores активными:

```text
retry
retry
retry
```

Поэтому:

> Lock-free не означает автоматически быстрее под любой нагрузкой.

Выбор требует benchmark конкретного workload.

---

## 32.15. Data race и business race

Race detector ищет data races памяти.

Но backend может иметь logical race при идеальной memory synchronization.

Например два database transactions читают один balance и выполняют lost update.

То есть:

```text
go test -race clean
```

не доказывает correctness concurrency на уровне DB/domain.

---

## 32.16. Почему race detector не доказывает отсутствие race

Detector dynamic.

Он наблюдает выполненные paths.

Если тест не создал опасное interleaving/path:

```text
race exists
но не executed
↓
no report
```

Поэтому нужны meaningful concurrent tests/load scenarios.

---

## 32.17. Memory Model как запрет «угадывать CPU»

Нельзя строить correctness на фразах:

```text
"на x86 так обычно видно"
"compiler вряд ли переставит"
"эта запись маленькая"
```

Correct Go program опирается на language synchronization guarantees.

Hardware detail нужен для performance, а не для замены Memory Model.

---

# Источники и ориентиры реализации
Для деталей текущей реализации использованы и рекомендуются официальные материалы проекта Go:

- `runtime/HACKING` — модель G/M/P и общие правила runtime;
- `runtime/proc.go` — scheduler, parking/wakeup, run queues, work stealing;
- `runtime/runtime2.go` — основные runtime structures;
- `runtime/netpoll.go` и platform netpoll implementations — network poller;
- `runtime/stack.go` — stack growth/copy/shrink;
- `runtime/chan.go` и `runtime/select.go` — channels, wait queues и `sudog`;
- `internal/sync/mutex.go` — текущая реализация Mutex fast/slow paths;
- `runtime/sema.go` — runtime semaphore machinery;
- Go Memory Model — language-level memory ordering guarantees;
- Go 1.25 release notes и material по container-aware `GOMAXPROCS` — актуальная container behaviour.

Численные thresholds, размеры внутренних queues, конкретный порядок scheduler heuristics и layout runtime structures следует считать implementation details конкретной версии Go, а не контрактом языка.
