import {
  useEffect,
  useRef,
  useState,
} from "react";

import {
  cancelChatRequest,
  closeChatSession,
  openChatSession,
  sendChat,
} from "../services/chatService";

import { validateImage } from "../utils/validateImage";
import { firebaseAuth } from "../auth/firebase";


const makeId = () => crypto.randomUUID();


function useChat() {
  const [question, setQuestion] = useState("");
  const [image, setImage] = useState(null);
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [session, setSession] = useState(null);
  const [repairWorkflow, setRepairWorkflow] = useState(null);
  const sending = useRef(false);

  const generation = useRef(0);
  const opening = useRef(null);
  const sessionRef = useRef(null);
  const messagesRef = useRef([]);
  const imageRef = useRef(null);

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    imageRef.current = image;
  }, [image]);


  function releaseImages() {
    messagesRef.current.forEach((message) => {
      if (message.image?.startsWith("blob:")) {
        URL.revokeObjectURL(message.image);
      }
    });

    if (imageRef.current?.previewUrl) {
      URL.revokeObjectURL(
        imageRef.current.previewUrl
      );
    }
  }


  async function ensureSession() {
    if (
      sessionRef.current &&
      sessionRef.current.expires_at * 1000 > Date.now()
    ) {
      return sessionRef.current;
    }

    if (!opening.current) {
      opening.current = openChatSession();
    }

    try {
      const opened = await opening.current;

      sessionRef.current = opened;
      setSession(opened);
      setRepairWorkflow(opened.repair_workflow || null);
      // A startup request can race Firebase session restoration. Clear any
      // transient authentication error once a valid session is opened.
      setError("");

      setMessages(
        opened.messages.map((message) => ({
          ...message,
          id: makeId(),

          ...(message.role === "assistant"
            ? {
                status: "done",
                error: "",
              }
            : {}),
        }))
      );

      return opened;
    } finally {
      opening.current = null;
    }
  }


  // Restore replies from the backend.
  // Only the session ID and expiry are stored in sessionStorage.
  useEffect(() => {
    let mounted = true;

    ensureSession().catch((err) => {
      if (mounted) {
        setError(err.message);
      }
    });

    return () => {
      mounted = false;
    };
  }, []);

  // The chat hook mounts before Firebase restores a browser session. Retry
  // session creation when the authenticated user becomes available.
  useEffect(() => {
    if (!firebaseAuth) return undefined;
    return firebaseAuth.onAuthStateChanged(nextUser => {
      if (nextUser && !sessionRef.current) {
        ensureSession().catch(() => {});
      }
    });
  }, []);


  useEffect(() => {
    if (!session) return;

    const remaining = Math.max(
      0,
      session.expires_at * 1000 - Date.now()
    );

    const timer = setTimeout(() => {
      generation.current += 1;

      cancelChatRequest();
      releaseImages();

      closeChatSession(
        session.session_id
      ).catch(() => {});

      sessionRef.current = null;

      setSession(null);
      setMessages([]);
      setRepairWorkflow(null);
      setImage(null);
      setQuestion("");
      setLoading(false);
      sending.current = false;

      setError(
        "Your one-hour session expired. " +
        "Start a new chat to continue."
      );
    }, remaining);

    return () => clearTimeout(timer);
  }, [session]);


  function selectImage(file) {
    const result = validateImage(file);

    if (!result.valid) {
      setError(result.error);
      return;
    }

    if (image) {
      URL.revokeObjectURL(image.previewUrl);
    }

    setImage({
      file,
      previewUrl: URL.createObjectURL(file),
    });

    setError("");
  }


  function removeImage() {
    if (image) {
      URL.revokeObjectURL(image.previewUrl);
    }

    setImage(null);
  }


  async function sendMessage(event, repairAction = null) {
    event?.preventDefault();

    const text = repairAction
      ? (repairAction.action === 'done' ? 'Done — current repair step completed.' : 'Issue resolved — create maintenance report.')
      : question.trim();

    if ((!text && !image) || loading || sending.current) {
      return;
    }

    const epoch = generation.current;
    const selectedImage = repairAction ? null : image;
    const assistantId = makeId();
    let assistantAdded = false;

    setLoading(true);
    sending.current = true;
    setError("");

    try {
      const current = await ensureSession();

      if (epoch !== generation.current) {
        return;
      }

      setMessages((previous) => [
        ...previous,

        {
          id: makeId(),
          role: "user",
          text,
          image: selectedImage?.previewUrl || null,
        },

        {
          id: assistantId,
          role: "assistant",
          status: "loading",
          response: null,
          error: "",
        },
      ]);
      assistantAdded = true;

      if (!repairAction) {
        setQuestion("");
        setImage(null);
      }

      const response = await sendChat({
        question: text,
        imageFile: selectedImage?.file,
        sessionId: current.session_id,
        requestTimeoutSeconds: current.request_timeout_seconds,
        repairAction,
      });

      if (epoch !== generation.current) {
        return;
      }

      setRepairWorkflow(response.repair_workflow || null);

      setMessages((previous) =>
        previous.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                status: "done",
                response,
              }
            : message
        )
      );
    } catch (err) {
      if (epoch !== generation.current) {
        return;
      }

      if (!assistantAdded) {
        setError(err.message);
      }

      setMessages((previous) =>
        previous.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                status: "error",
                error: err.message,
              }
            : message
        )
      );
    } finally {
      if (epoch === generation.current) {
        sending.current = false;
        setLoading(false);
      }
    }
  }


  async function resetChat() {
    generation.current += 1;

    cancelChatRequest();
    releaseImages();

    const oldSession = sessionRef.current;

    sessionRef.current = null;

    setSession(null);
    setMessages([]);
    setRepairWorkflow(null);
    setImage(null);
    setQuestion("");
    setLoading(false);
    sending.current = false;
    setError("");

    try {
      await closeChatSession(
        oldSession?.session_id
      );

      await ensureSession();
    } catch (err) {
      setError(err.message);
    }
  }


  return {
    question,
    setQuestion,
    image,
    messages,
    loading,
    error,
    selectImage,
    removeImage,
    sendMessage,
    resetChat,
    repairWorkflow,
    sendRepairAction: action => sendMessage(null, action),
  };
}


export default useChat;
