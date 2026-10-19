# Manufacturing Equipment Copilot — Frontend

React/Vite chat interface for questions, optional equipment photos and structured manual answers.

Read the complete [Frontend Guide](../FRONTEND_GUIDE.md) for the component map, state and session handling, API contract, image lifecycle, response rendering, troubleshooting and full question-to-answer flow.

Run from the repository root:

```powershell
npm.cmd --prefix frontend ci
.\frontend\run.cmd
```

Start the backend in another terminal with `.\backend\run.cmd`. Open `http://localhost:5173`; the default API is `http://127.0.0.1:8000`.

Set `VITE_API_URL` in `frontend/.env.local` to use another backend, then restart Vite.

Checks, from the repository root:

```powershell
node --test frontend/tests/chatService.test.mjs
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run build
```
