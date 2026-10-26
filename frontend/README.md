# Manufacturing Equipment Copilot frontend: the code flow

This guide explains how the website works in simple language. It follows the
screen from startup, through a question or photo, to the answer shown in the
chat.

## The big picture

The frontend is the part of the app you can see and click. It does not search
the manuals or write the technical answer itself. It sends your question to
the backend, then displays the backend's answer.

```text
                         WEBSITE STARTS
                              |
                              v
+----------------------------------------------+
| Browser loads the page and React entry point |
| Files: index.html, src/main.jsx               |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Build the main app and load the chat styles  |
| File: src/App.jsx                            |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Check login and account approval settings    |
| File: src/auth/AuthGate.jsx                  |
+----------------------------------------------+
                              |
                              | If allowed into the chat
                              v
+----------------------------------------------+
| Create the chat screen and connect its parts |
| File: src/App.jsx                            |
| Component: ChatWorkspace                     |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Keep track of the question, photo, messages, |
| loading state, session, and repair workflow  |
| File: src/hooks/useChat.js                   |
| Hook: useChat()                              |
+----------------------------------------------+
                              |
          +-------------------+------------------+
          |                                      |
          v                                      v
+--------------------------------+  +--------------------------------+
| Show the question box and      |  | Show messages, progress,       |
| optional photo preview        |  | errors, and repair controls    |
| File: components/chat/        |  | Files: components/chat/         |
| ChatInput.jsx                 |  | ChatThread.jsx, ChatMessage.jsx|
+--------------------------------+  +--------------------------------+
          |                                      ^
          | You press Send                       |
          v                                      |
+----------------------------------------------+ |
| Check the photo, prepare the request, and    | |
| send it to the backend                       | |
| File: src/services/chatService.js            | |
| Function: sendChat()                          | |
+----------------------------------------------+ |
          |                                      |
          | HTTP request to backend             |
          v                                      |
+----------------------------------------------+ |
| Backend answers and returns JSON             | |
| Backend route: POST /chat                    | |
+----------------------------------------------+ |
          |                                      |
          | Answer JSON comes back              |
          v                                      |
+----------------------------------------------+ |
| Put the answer into the right chat message   | |
| File: src/hooks/useChat.js                   | |
| Function: sendMessage()                      | |
+----------------------------------------------+ |
          |                                      |
          v                                      |
+----------------------------------------------+ |
| Choose how to display each answer block      | |
| File: components/chat/ResponseRenderer.jsx   | |
| Shows text, tables, manual images, links,    | |
| safety notes, and repair controls            | |
+----------------------------------------------+ |
          |                                      |
          +--------------------------------------+
                       Screen updates
```

The arrows show the usual order: one part hands work to the next. The user
clicking **Send** is what starts the trip from the question box to the backend.

## What happens when you open the website

1. `index.html` provides the page's root element. `src/main.jsx` starts React
   and asks it to show `App`.
2. `src/App.jsx` loads the main styles and places the chat inside
   `AuthGate`.
3. `src/auth/AuthGate.jsx` handles login and account status when authentication
   is enabled. If authentication is disabled in the frontend configuration, it
   lets the chat open directly. With authentication enabled, users must meet
   the configured account requirements before seeing the chat.
4. `ChatWorkspace` in `src/App.jsx` calls `useChat()` and gives its state and
   actions to the chat screen components.
5. `src/hooks/useChat.js` opens or restores a backend conversation session.
   The session ID and expiry are kept in browser `sessionStorage`; the
   conversation history itself is fetched from the backend.

## What happens when you send a question

1. **Type or attach a photo.** `src/components/chat/ChatInput.jsx` displays the
   question field and the photo preview. The image upload component lets you
   choose a photo.
2. **Check the photo.** `src/hooks/useChat.js` calls
   `src/utils/validateImage.js` to check an uploaded image, then creates a
   temporary preview for the screen.
3. **Show that work has started.** The hook adds your message and a temporary
   assistant message to the chat, then marks the screen as loading.
4. **Send the request.** `src/services/chatService.js` builds a form containing
   the session ID, question, optional image, and any repair-action details. It
   adds the signed-in user's access token when available and sends the request
   to the backend's `/chat` endpoint.
5. **Show progress.** While the backend is working, the service can poll the
   session progress endpoint. `useChat.js` updates the temporary assistant
   message with the latest stage and any available safety guidance.
6. **Receive the answer.** When the backend returns JSON, `chatService.js`
   checks for HTTP errors and makes image URLs ready to load. `useChat.js`
   replaces the temporary assistant message with the completed response.
7. **Display the answer.** `src/components/chat/ChatThread.jsx` arranges the
   conversation, and `src/components/chat/ChatMessage.jsx` decides whether to
   show a user message, loading state, error, or completed answer.
   `src/components/chat/ResponseRenderer.jsx` displays each answer block.

## How different answer parts appear

`src/components/chat/ResponseRenderer.jsx` chooses how to show the response:

- **Text** is shown as Markdown, such as headings and lists.
- **Tables** are drawn as HTML tables.
- **Manual images** are fetched from the backend using the user's access token.
  They can be opened in a larger preview.
- **Links and videos** are shown as clickable links when their addresses are
  safe to open.
- **Safety notes** are shown with repair answers.
- **Repair actions** are displayed with controls when there is an active repair
  step.
- **Sources and warnings** are shown when the backend included them.

## How guided repair controls work

1. The backend sends the current repair step and its workflow identifiers.
2. `ChatThread.jsx` and `ChatMessage.jsx` identify the active repair message.
3. `ResponseRenderer.jsx` displays the answer and renders
   `src/components/chat/RepairControls.jsx`.
4. The technician can choose **Done**, add an observation or image, report a
   problem, or report that the issue is resolved.
5. The control sends that choice to `useChat.js`, which passes it through
   `chatService.js` to the backend. Workflow ID, step ID, and plan version help
   the backend confirm that the action belongs to the current step.
6. The updated response is displayed as the next message. Earlier messages
   stay in the conversation as history.

## Other useful frontend files

| File | In simple terms |
|---|---|
| `src/main.jsx` | Starts React in the browser page. |
| `src/App.jsx` | Puts together the authentication gate and chat screen. |
| `src/auth/AuthGate.jsx` | Handles login and account approval screens. |
| `src/hooks/useChat.js` | Holds chat state and coordinates sending, retrying, and reset. |
| `src/services/chatService.js` | Communicates with the backend API. |
| `src/config/appConfig.js` | Holds the backend address and common frontend settings. |
| `src/components/chat/ChatInput.jsx` | Shows the question field, image preview, and Send button. |
| `src/components/chat/ChatThread.jsx` | Arranges all messages and active repair state. |
| `src/components/chat/ChatMessage.jsx` | Displays one user or assistant message. |
| `src/components/chat/ResponseRenderer.jsx` | Displays text, tables, images, links, sources, and repair controls. |
| `src/components/chat/RepairControls.jsx` | Shows buttons and fields for repair-step feedback. |
| `src/components/chat/Documentation.jsx` | Displays the maintenance report editing and export interface. |
| `src/services/documentationApi.js` | Sends maintenance report requests to the backend. |
| `src/auth/authApi.js` | Sends authentication and profile-related API requests. |

## Frontend and backend are separate

```text
Your browser                                  Backend server
------------                                  --------------
React shows the question
       |
       | Sends question, photo, and session ID
       +-------------------------------------> Searches manuals and creates answer
       |                                      |
       | Receives answer JSON                 |
       <-------------------------------------+
Shows the answer on the screen
```

The browser does not open PDFs or search BigQuery. The ingestion pipeline
prepares manuals beforehand; the backend searches those prepared records when
you ask a question.

## Run the frontend locally

From the repository root, install packages once and start the frontend:

```powershell
npm.cmd --prefix frontend ci
.\frontend\run.cmd
```

Start the backend in a second PowerShell window:

```powershell
.\backend\run.cmd
```

Open `http://localhost:5173` in your browser. The frontend's default backend
address is `http://127.0.0.1:8001`, as configured in
`src/config/appConfig.js`. To use another backend, set `VITE_API_URL` in
`frontend/.env.local` and restart the frontend.

## Run frontend checks

From the repository root:

```powershell
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run build
```
