import { APP_CONFIG } from "../config/appConfig";
import { logPerformance } from '../utils/performanceLog';
import { firebaseAuth } from '../auth/firebase';
import { chatErrorMessage } from '../utils/chatErrorMessage';


const SESSION_KEY = "maintenance-chat-session-v2";
const PROGRESS_POLL_INITIAL_DELAY_MS = 400;
const PROGRESS_RECONNECT_DELAY_MS = 75;

function sessionStorageKey() {
  // Firebase UID separates sessions when accounts share the same browser tab.
  return `maintenance-chat-session-v3:${firebaseAuth?.currentUser?.uid || 'local'}`;
}

export function forgetStoredChatSession() {
  sessionStorage.removeItem(sessionStorageKey());
  sessionStorage.removeItem(SESSION_KEY);
}

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

  const error = new Error(chatErrorMessage(message, response.status));
  error.status = response.status;
  error.requestId = response.headers?.get('X-Request-ID') || null;

  throw error;
}


function remember(session, storageKey) {
  sessionStorage.setItem(
    storageKey,
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
  const storageKey = sessionStorageKey();
  // Old, unscoped session IDs cannot safely be restored for a different user.
  sessionStorage.removeItem(SESSION_KEY);
  let saved = null;

  try {
    saved = JSON.parse(
      sessionStorage.getItem(storageKey)
    );
  } catch {
    sessionStorage.removeItem(storageKey);
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

  sessionStorage.removeItem(storageKey);

  const session = await checked(await fetch(`${APP_CONFIG.apiBaseUrl}/sessions`,
    { method: "POST", headers: await authHeaders() }));

  remember(session, storageKey);

  return {
    ...session,
    messages: [],
  };
}


export function cancelChatRequest() {
  activeController?.abort();
  activeController = null;
}

export async function getChatSession(sessionId, signal) {
  const session = await checked(await fetch(`${APP_CONFIG.apiBaseUrl}/sessions/${sessionId}`, {headers:await authHeaders(),signal}));
  return {...session,messages:(session.messages || []).map(message => message.response ? {...message,response:absoluteImages(message.response)} : message)};
}

export async function resumeChatSession(sessionId, requestId, onProgress, signal) {
  let cursor = '';
  while (!signal.aborted) {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
    const progress = await checked(await fetch(`${APP_CONFIG.apiBaseUrl}/sessions/${sessionId}/progress/${requestId}${query}`, { headers: await authHeaders(), signal }));
    cursor = progress.cursor || '';
    onProgress(progress);
    if (progress.status !== 'processing') {
      const session = await getChatSession(sessionId, signal);
      return {...session, interrupted:progress.status !== 'completed'};
    }
    await new Promise(resolve => setTimeout(resolve, PROGRESS_RECONNECT_DELAY_MS));
  }
  return null;
}


export async function closeChatSession(sessionId) {
  cancelChatRequest();

  forgetStoredChatSession();

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
  onProgress,
  clientRequestId: suppliedRequestId,
  activeWorkflow,
  startNewCase = false,
}) {
  const storageKey = sessionStorageKey();
  const form = new FormData();

  form.append("session_id", sessionId);
  form.append("message", question || "");
  if (startNewCase) form.append('start_new_case', 'true');
  if (repairAction) {
    form.append('repair_action', repairAction.action);
    for (const name of ['workflow_id', 'step_id', 'machine', 'issue', 'cause', 'action_taken', 'issue_report', 'plan_version', 'observation', 'safety_confirmed']) {
      form.append(name, repairAction[name] ?? (name === 'plan_version' ? 1 : name === 'safety_confirmed' ? false : ''));
    }
  }
  else if (activeWorkflow) {
    form.append('workflow_id', activeWorkflow.workflow_id);
    form.append('step_id', activeWorkflow.current_step?.step_id || '');
    form.append('plan_version', activeWorkflow.plan_version || 1);
  }

  if (imageFile) {
    form.append(
      "image",
      imageFile,
      imageFile.name
    );
  }

  const controller = new AbortController();
  const clientRequestId = suppliedRequestId || crypto.randomUUID();
  const clientStarted = performance.now();
  let requestId = null;
  activeController = controller;
  let timedOut = false;
  let finished = false;
  let progressTimer;
  let progressCursor = '';
  const progressController = new AbortController();
  async function pollProgress() {
    if (finished || controller.signal.aborted || !onProgress) return;
    try {
      const query = progressCursor ? `?cursor=${encodeURIComponent(progressCursor)}` : '';
      const response = await fetch(`${APP_CONFIG.apiBaseUrl}/sessions/${encodeURIComponent(sessionId)}/progress/${encodeURIComponent(clientRequestId)}${query}`, { headers: await authHeaders(), signal: progressController.signal });
      if (response.ok && !finished) {
        const progress = await response.json();
        progressCursor = progress.cursor || '';
        onProgress(progress);
      }
    } catch { /* Main request remains authoritative if a progress poll fails. */ }
    if (!finished) progressTimer = setTimeout(pollProgress, PROGRESS_RECONNECT_DELAY_MS);
  }
  if (onProgress) progressTimer = setTimeout(pollProgress, PROGRESS_POLL_INITIAL_DELAY_MS);
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

    remember(result, storageKey);

    return absoluteImages(result);
  } catch (error) {
    logPerformance('chat-error', {request_id: requestId || error.requestId || null,
      client_request_id: clientRequestId,
      elapsed_ms: performance.now()-clientStarted, status: error.status || null,
      reason: timedOut ? 'client_timeout' : error.name});
    if (error.status === 410) {
      sessionStorage.removeItem(storageKey);
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
        "Connection interrupted. Please check your connection and retry."
      );
    }

    throw error;
  } finally {
    finished = true;
    progressController.abort();
    clearTimeout(progressTimer);
    clearTimeout(timeout);

    if (activeController === controller) {
      activeController = null;
    }
  }
}
