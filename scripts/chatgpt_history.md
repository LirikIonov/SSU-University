# Выгрузка истории ChatGPT

Когда ChatGPT пишет **«Вы достигли максимальной длины чата»**, а историю нужно сохранить целиком, используется следующий скрипт для выгрузки архива сообщений из веб-версии ChatGPT.

Скрипт проходит историю **сверху вниз**, собирает сообщения пользователя и ассистента и сохраняет их в отдельный файл.

> **Важно:** сам скрипт ниже оставлен без изменений.

## Скрипт

```javascript
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  const first = document.querySelector(
    '[data-message-author-role]'
  );

  if (!first) {
    throw new Error('Сообщения не найдены');
  }

  // Находим реальный скроллер чата.
  let scroller = first.parentElement;

  while (scroller && scroller !== document.body) {
    const style = getComputedStyle(scroller);

    if (
      /(auto|scroll)/.test(style.overflowY) &&
      scroller.scrollHeight > scroller.clientHeight
    ) {
      break;
    }

    scroller = scroller.parentElement;
  }

  scroller ||= document.scrollingElement;

  console.log('Scroller:', scroller);

  const seen = new Set();
  const messages = [];

  function collect() {
    const nodes = document.querySelectorAll(
      '[data-message-author-role]'
    );

    for (const node of nodes) {
      const role = node.getAttribute(
        'data-message-author-role'
      );

      if (
        role !== 'user' &&
        role !== 'assistant'
      ) {
        continue;
      }

      const turn = node.closest(
        '[data-testid^="conversation-turn-"]'
      );

      const id =
        turn?.getAttribute('data-testid') ||
        node.getAttribute('data-message-id');

      const text = node.innerText.trim();

      if (!text) {
        continue;
      }

      // conversation-turn-N — нормальный уникальный ID.
      // Fallback нужен на случай изменения DOM ChatGPT.
      const key = id || `${role}:${text}`;

      if (seen.has(key)) {
        continue;
      }

      seen.add(key);

      messages.push({
        role,
        text
      });
    }
  }

  // Начинаем именно СВЕРХУ.
  scroller.scrollTop = 0;

  await sleep(500);

  collect();

  let previousTop = -1;
  let stable = 0;

  while (stable < 5) {
    const before = scroller.scrollTop;

    // Почти целый экран за один шаг.
    scroller.scrollTop +=
      scroller.clientHeight * 0.9;

    // Только небольшая пауза для React.
    await sleep(80);

    collect();

    const after = scroller.scrollTop;

    if (
      after === before ||
      after === previousTop ||
      after + scroller.clientHeight >=
        scroller.scrollHeight - 10
    ) {
      stable++;
    } else {
      stable = 0;
    }

    previousTop = after;

    if (messages.length % 20 === 0) {
      console.log(
        `Собрано: ${messages.length}`
      );
    }
  }

  // Несколько последних проходов на случай,
  // если React дорисовал конец с задержкой.
  for (let i = 0; i < 5; i++) {
    scroller.scrollTop = scroller.scrollHeight;
    await sleep(150);
    collect();
  }

  const text = messages
    .map(m => {
      const author =
        m.role === 'user'
          ? 'USER'
          : 'ASSISTANT';

      return (
        `${author}\n\n` +
        `${m.text}`
      );
    })
    .join('\n\n' + '='.repeat(80) + '\n\n');

  const blob = new Blob(
    [text],
    {
      type: 'text/plain;charset=utf-8'
    }
  );

  const url = URL.createObjectURL(blob);

  const a = document.createElement('a');

  a.href = url;
  a.download = 'chatgpt-history.txt';

  document.body.appendChild(a);
  a.click();
  a.remove();

  URL.revokeObjectURL(url);

  console.log(
    `ГОТОВО. Выкачано сообщений: ${messages.length}`
  );
})();
```
