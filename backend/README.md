# Manufacturing Copilot backend: the code flow

This guide explains what the backend does in beginner-friendly language. It
follows a chat question from the moment the server starts to the moment the
answer is sent back.

## The big picture

Think of the backend as a kitchen:

- The **API** is the counter where a question arrives.
- The **planner** works out what kind of question it is.
- **BigQuery** is the shelf of searchable manual information.
- The **agent** prepares an answer using the manual information it was given.
- The **response builder** checks and packages the answer before it is served.

```text
                         SERVER STARTS
                              |
                              v
+----------------------------------------------+
| Create the web server and connect its routes |
| File: app/main.py                            |
| Object: app                                  |
+----------------------------------------------+
                              |
                              | The frontend creates a conversation
                              v
+----------------------------------------------+
| Create a chat session                        |
| File: app/api/session_routes.py              |
| Function: create_session()                   |
+----------------------------------------------+
                              |
                              | The user sends a question or photo
                              v
+----------------------------------------------+
| Receive the chat request                     |
| File: app/api/chat_routes.py                 |
| Function: chat()                             |
| Check access, session, text, and photo        |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Coordinate the answer process                |
| File: app/agent/chat_service.py              |
| Function: answer_question()                  |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Understand the question and choose what to   |
| search for                                   |
| File: app/agent/planner.py                   |
| Function: understand()                       |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Turn the search wording into numbers         |
| (an embedding)                               |
| File: app/retrieval/embeddings.py            |
| Function: get_embedding()                    |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Find matching manual chunks in BigQuery      |
| File: app/retrieval/manual_search.py         |
| Function: search_manual()                    |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Load related manual images, pages, and links |
| File: app/media/prepare_media.py             |
| Function: prepare_media()                    |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Give the question and approved evidence to   |
| the Google ADK/Gemini agent                  |
| File: app/agent/chat_service.py              |
| Agent definitions: app/agent/agent.py        |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Check the answer format, evidence IDs, and   |
| allowed images/links; format the result      |
| File: app/responses/response_builder.py      |
| Function: final_blocks()                     |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Check the agent's structured answer and      |
| confirm its evidence IDs are known           |
| File: app/agent/chat_service.py              |
| Schema: app/responses/schemas.py             |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Turn the checked answer into text, table,    |
| image, link, and source blocks               |
| File: app/responses/response_builder.py      |
| Function: final_blocks()                     |
+----------------------------------------------+
                              |
                              v
+----------------------------------------------+
| Save the reply and conversation snapshot,    |
| then return JSON to the frontend             |
| Files: app/api/chat_routes.py,               |
|        app/sessions/session_store.py,        |
|        app/sessions/persistence.py           |
+----------------------------------------------+
                              |
                              v
                  FRONTEND SHOWS THE ANSWER
```

The main diagram shows a normal manual question. Two shortcuts are worth
knowing:

- A question asking which manuals are available skips the planner, embedding,
  media, and answer agent. `app/agent/chat_service.py` calls
  `list_available_documents()` in `app/retrieval/manual_search.py` and returns
  the list directly.
- For a repair request, safety guidance is prepared independently while the
  normal evidence-and-answer flow continues. This happens in
  `app/agent/safety.py`; it does not replace the manual search or answer.

## Following one normal question

1. **The server starts.** `app/main.py` creates the FastAPI web application and
   connects the chat, session, image, health, authentication, and report routes.
2. **A conversation is created.** The frontend asks
   `app/api/session_routes.py` for a session ID. The session store keeps the
   conversation's history and state.
3. **A question arrives.** `app/api/chat_routes.py` checks that the user is
   allowed to use the app, that the session belongs to them, and that the
   question or uploaded photo is acceptable.
4. **The coordinator takes over.** `app/agent/chat_service.py` calls
   `answer_question()`. It handles the order of work and keeps track of the
   current conversation and any repair workflow.
5. **The planner interprets the request.** `app/agent/planner.py` decides
   whether the user wants manual information, troubleshooting, a follow-up, an
   image, a video, or clarification. It makes a search plan; it does not supply
   the technical answer.
6. **The manual is searched.** `app/retrieval/embeddings.py` turns the search
   wording into a list of numbers that represents its meaning.
   `app/retrieval/manual_search.py` uses those numbers and keyword matches to
   find likely manual chunks in BigQuery.
7. **Related media is prepared.** `app/media/prepare_media.py` loads approved
   manual images and links related to the results. A photo from the user may
   help the agent understand the question, but it is not treated as approved
   manual evidence.
8. **The answer is generated.** `app/agent/chat_service.py` sends the question,
   conversation context, selected evidence, and permitted media to the agent
   defined in `app/agent/agent.py`. Repair requests and image questions use the
   appropriate multimodal model path; smaller text-only questions can use a
   lighter model.
9. **The answer is checked and packaged.** `app/agent/chat_service.py` checks
   the answer against `app/responses/schemas.py` and makes sure its evidence
   IDs are known. `app/responses/response_builder.py` checks that referenced
   media was supplied and builds the text, table, image, link, and source
   blocks.
10. **The reply is saved and returned.** The chat route adds the request and
    response to the conversation, saves a snapshot, and returns JSON for the
    frontend to display.

## How a repair request is different

For a real repair or troubleshooting request, the backend also prepares safety
prerequisites using `app/agent/safety.py`. This work can happen while the
manual search and answer are being prepared. Safety guidance is not proof that
the equipment has been isolated.

The repair workflow in `app/agent/repair_workflow.py` tracks the active issue,
current step, completed steps, and technician observations. The agent can
prepare later steps privately, but the backend shows one step at a time. When
the technician presses **Done**, the server validates that the action belongs
to the current workflow and step before advancing. If the technician reports
that a step did not work, the backend can request a revised plan.

## What the main backend files do

| File | In simple terms |
|---|---|
| `app/main.py` | Starts the web server and connects its routes. |
| `app/api/chat_routes.py` | Receives chat questions and returns answers. |
| `app/api/session_routes.py` | Creates, reads, deletes, and reports progress for conversations. |
| `app/agent/chat_service.py` | Coordinates planning, manual search, media, the model, and repair state. |
| `app/agent/planner.py` | Understands the request and creates a search plan. |
| `app/agent/agent.py` | Defines the answer-generating agents and model settings. |
| `app/agent/safety.py` | Prepares safety prerequisites for repair requests. |
| `app/agent/repair_workflow.py` | Tracks repair steps and validates technician actions. |
| `app/retrieval/embeddings.py` | Turns question wording into search numbers. |
| `app/retrieval/manual_search.py` | Finds relevant manual chunks and associated data in BigQuery. |
| `app/media/prepare_media.py` | Loads approved images and links for the answer. |
| `app/responses/schemas.py` | Defines the expected shapes of plans, actions, and answers. |
| `app/responses/response_builder.py` | Checks and packages answer blocks and citations. |
| `app/sessions/session_store.py` | Keeps active conversation state and history available to the app. |
| `app/sessions/persistence.py` | Saves and restores conversation snapshots and documents. |

Other routes handle user authentication, health checks, image delivery, and
maintenance reports. They are connected in `app/main.py` but are not part of
the ordinary question-to-answer path.

## Run the backend locally

From the project root, start the backend with:

```powershell
.\backend\run.cmd
```

The launcher runs Uvicorn with `app.main:app` from the `backend` directory. In
this repository it uses port `8001`. The frontend normally sends its chat and
session requests to this backend.

## Important idea: the backend does not read a PDF during each chat

The manual ingestion pipeline and the chat backend are separate stages:

```text
PDF ingestion pipeline                    Chat backend
---------------------                    -------------
PDF -> extracted chunks -> BigQuery  ---> question -> search those chunks
                                             -> answer from selected evidence
```

The ingestion pipeline prepares manuals and loads them into BigQuery ahead of
time. During a chat, the backend searches the already-loaded manual data; it
does not rerun the PDF extraction pipeline for every question.
