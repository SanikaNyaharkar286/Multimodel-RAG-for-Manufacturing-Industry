import { APP_CONFIG } from "../config/appConfig";


const SESSION_KEY = "maintenance-chat-session-v2";

let activeController = null;


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
      encodeURIComponent(saved.session_id)
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

  const session = await checked(
    await fetch(
      `${APP_CONFIG.apiBaseUrl}/sessions`,
      { method: "POST" }
    )
  );

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
    { method: "DELETE" }
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
}) {
  const form = new FormData();

  form.append("session_id", sessionId);
  form.append("message", question || "");

  if (imageFile) {
    form.append(
      "image",
      imageFile,
      imageFile.name
    );
  }

  const controller = new AbortController();
  activeController = controller;

  const timeout = setTimeout(
    () => controller.abort(),
    135000
  );

  try {
    const response = await fetch(
      `${APP_CONFIG.apiBaseUrl}${APP_CONFIG.chatEndpoint}`,
      {
        method: "POST",
        body: form,
        signal: controller.signal,
      }
    );

    const result = await checked(response);

    remember(result);

    return absoluteImages(result);
  } catch (error) {
    if (error.status === 410) {
      sessionStorage.removeItem(SESSION_KEY);
    }

    if (error.name === "AbortError") {
      throw new Error(
        "The request was cancelled or timed out."
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