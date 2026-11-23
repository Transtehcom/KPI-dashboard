# Проверка модуля Sites

Из корня Rust-копии выполните следующие команды с Node.js 22 или новее и установленной целью Rust `wasm32-unknown-unknown`. Для `test:worker` требуется версия Node с `node:sqlite`.

```text
npm ci --ignore-scripts
npm run build:worker
npm run test:worker
npm run test:workerd
```

Сборка компилирует Rust и преобразует полученный Wasm в ES-модуль с помощью Binaryen для запуска в Sites. Тест последовательно открывает все разделы, сравнивает ответы с исходным Wasm и проверяет изоляцию и конкурентную запись. Следующий тест запускает поставляемый модуль в Cloudflare workerd с собственной временной D1, применяет SQL-миграцию и проверяет действительные HTTP-ответы.

Для сравнения с Python сначала запишите эталон по инструкции в [verification/http](http/README.md). Из каталога записи, где находятся `rust/app-cases` и `store`, выполните команду с абсолютным путём к этой копии.

```text
rust-anything app replay . --build "node <путь-к-Rust-копии>/verification/workerd-oracle.mjs" --timeout 40 --json
```

Тестовый HTTP-мост запускается только на loopback. Он подставляет заголовок удостоверения, который в приватном Sites выставляет хостинг, и создаёт три учебных сессии в локальной D1.
