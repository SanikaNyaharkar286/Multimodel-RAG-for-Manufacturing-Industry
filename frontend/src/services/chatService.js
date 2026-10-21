import { APP_CONFIG } from "../config/appConfig";
import { logPerformance } from '../utils/performanceLog';
import { firebaseAuth } from '../auth/firebase';


const SESSION_KEY = "maintenance-chat-session-v2";

let activeController = null;

async function authHeaders() {
  let user = firebaseAuth?.currentUser;
  if (!user && firebaseAuth) {
    user = await new Promise(resolve => {
      let settled = false;
      const unsubscribe = firebaseAuth.onAuthStateChanged(next => {
        if (settled) return;
        settled = true;
        unsubscribe();
        resolve(next);
      });
      setTimeout(() => { if (!settled) { settled = true; unsubscribe(); resolve(firebaseAuth.currentUser); } }, 5000);
    });
  }
  const token = user ? await user.getIdToken() : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}


async function checked(response) {
  if (response.ok) {
    return response.json();
  }

  let message = `Server error (${response.status})`;

  try {
    const body = await response.json();

    if (typeof body.detail === "string") {
      message = body.detail;
    }
  } catch {
    // Keep the HTTP error when the response is not JSON.
  }

  const error = new Error(message);
  error.status = response.status;
  error.requestId = response.headers?.get('X-Request-ID') || null;

  throw error;
}


function remember(session) {
  sessionStorage.setItem(
    SESSION_KEY,
    JSON.stringify({
      session_id: session.session_id,
      expires_at: session.expires_at,
    })
  );
}


function absoluteImages(response) {
  return {
    ...response,

    blocks: (response.blocks || []).map((block) =>
      block.type === "image"
        ? {
            ...block,
            src: new URL(
              block.src,
              APP_CONFIG.apiBaseUrl
            ).href,
          }
        : block
    ),
  };
}


export async function openChatSession() {
  let saved = null;

  try {
    saved = JSON.parse(
      sessionStorage.getItem(SESSION_KEY)
    );
  } catch {
    sessionStorage.removeItem(SESSION_KEY);
  }

  if (
    saved?.session_id &&
    saved.expires_at * 1000 > Date.now()
  ) {
    const response = await fetch(
      `${APP_CONFIG.apiBaseUrl}/sessions/` +
      encodeURIComponent(saved.session_id), { headers: await authHeaders() }
    );

    if (response.ok) {
      const session = await response.json();

      return {
        ...session,

        messages: (session.messages || []).map(
          (message) =>
            message.response
              ? {
                  ...message,
                  response: absoluteImages(
                    message.response
                  ),
                }
              : message
        ),
      };
    }

    if (![404, 410].includes(response.status)) {
      return checked(response);
    }
  }

  sessionStorage.removeItem(SESSION_KEY);

  const session = await checked(await fetch(`${APP_CONFIG.apiBaseUrl}/sessions`,
    { method: "POST", headers: await authHeaders() }));

  remember(session);

  return {
    ...session,
    messages: [],
  };
}


export function cancelChatRequest() {
  activeController?.abort();
  activeController = null;
}


export async function closeChatSession(sessionId) {
  cancelChatRequest();

  sessionStorage.removeItem(SESSION_KEY);

  if (!sessionId) return;

  const response = await fetch(
    `${APP_CONFIG.apiBaseUrl}/sessions/` +
    encodeURIComponent(sessionId),
    { method: "DELETE", headers: await authHeaders() }
  );

  if (
    !response.ok &&
    ![404, 410].includes(response.status)
  ) {
    await checked(response);
  }
}


export async function sendChat({
  question,
  imageFile,
  sessionId,
  requestTimeoutSeconds = 120,
  repairAction,
}) {
  const form = new FormData();

  form.append("session_id", sessionId);
  form.append("message", question || "");
  if (repairAction) {
    form.append('repair_action', repairAction.action);
    for (const name of ['workflow_id', 'step_id', 'machine', 'issue', 'cause', 'action_taken']) {
      form.append(name, repairAction[name] || '');
    }
  }

  if (imageFile) {
    form.append(
      "image",
      imageFile,
      imageFile.name
    );
  }

  const controller = new AbortController();
  const clientRequestId = crypto.randomUUID();
  const clientStarted = performance.now();
  let requestId = null;
  activeController = controller;
  let timedOut = false;
  const serverTimeout = Number(requestTimeoutSeconds);
  const timeoutMs = ((Number.isFinite(serverTimeout) && serverTimeout > 0
    ? serverTimeout : 120) + 15) * 1000;

  const timeout = setTimeout(
    () => {
      timedOut = true;
      controller.abort();
    },
    timeoutMs
  );

  try {
    const authToken = firebaseAuth?.currentUser
      ? await firebaseAuth.currentUser.getIdToken()
      : null;
    const response = await fetch(
      `${APP_CONFIG.apiBaseUrl}${APP_CONFIG.chatEndpoint}`,
      {
        method: "POST",
        headers: {'X-Client-Request-ID': clientRequestId,
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {})},
        body: form,
        signal: controller.signal,
      }
    );

    const headersAt = performance.now();
    requestId = response.headers?.get('X-Request-ID') || null;
    const result = await checked(response);
    const parsedAt = performance.now();
    result.request_id = requestId || result.request_id;
    result.client_timing = {
      request_id: result.request_id,
      client_request_id: clientRequestId,
      fetch_to_headers_ms: headersAt - clientStarted,
      body_and_json_ms: parsedAt - headersAt,
      fetch_total_ms: parsedAt - clientStarted,
      server_timing: response.headers?.get('Server-Timing') || null,
      response_received_at: parsedAt,
    };
    logPerformance('chat-api', result.client_timing);

    remember(result);

    return absoluteImages(result);
  } catch (error) {
    logPerformance('chat-error', {request_id: requestId || error.requestId || null,
      client_request_id: clientRequestId,
      elapsed_ms: performance.now()-clientStarted, status: error.status || null,
      reason: timedOut ? 'client_timeout' : error.name});
    if (error.status === 410) {
      sessionStorage.removeItem(SESSION_KEY);
    }

    if (error.name === "AbortError") {
      throw new Error(
        timedOut
          ? "The answer took too long. Please try again."
          : "The request was cancelled."
      );
    }

    if (error instanceof TypeError) {
      throw new Error(
        "Cannot connect to the backend. " +
        "Check that it is running on port 8000."
      );
    }

    throw error;
  } finally {
    clearTimeout(timeout);

    if (activeController === controller) {
      activeController = null;
    }
  }
}
