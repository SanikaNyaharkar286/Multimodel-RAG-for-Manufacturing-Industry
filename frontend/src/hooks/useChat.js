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
import { detectSafetyGuidance } from "../config/safetyGuidance";


const makeId = () => crypto.randomUUID();
const ANSWER_DISPLAY_DELAY_MS = 10_000;


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

      setMessages(previous => previous.length ? previous : opened.messages.map((message) => ({
        ...message,
        id: makeId(),
        ...(message.role === "assistant" ? { status: "done", error: "" } : {}),
      })));

      return opened;
    } finally {
      opening.current = null;
    }
  }


  // Restore replies from the backend.
  // Only the session ID and expiry are stored in sessionStorage.
  useEffect(() => {
    let mounted = true;

    // AuthGate mounts the workspace only after the profile is approved.
    ensureSession().catch((err) => {
      if (mounted) {
        setError(err.message);
      }
    });

    return () => {
      mounted = false;
    };
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

    const typedDone = !repairAction && !image
      && ['done', 'completed'].includes(question.trim().toLowerCase().replace(/[.!]+$/, ''))
      && repairWorkflow?.status === 'awaiting_done'
      && repairWorkflow.current_step?.step_id;
    if (typedDone) {
      repairAction = { action: 'done', workflow_id: repairWorkflow.workflow_id,
        step_id: repairWorkflow.current_step.step_id };
    }

    const text = repairAction
      ? (repairAction.action === 'done'
        ? 'Done — current repair step completed.'
        : repairAction.action === 'report_issue'
          ? `Did not fix — ${repairAction.issue_report || 'reporting the current result.'}`
          : 'Issue resolved — create maintenance report.')
      : question.trim();

    if ((!text && !image) || loading || sending.current) {
      return;
    }

    const epoch = generation.current;
    const selectedImage = repairAction?.action === 'report_issue'
      ? (repairAction.imageFile ? { file: repairAction.imageFile, previewUrl: repairAction.imagePreviewUrl || null } : null)
      : (repairAction ? null : image);
    const assistantId = makeId();
    const answerStartedAt = performance.now();
    const hasPreviousSafety = messagesRef.current.some(message =>
      message.safetyGuidance || message.response?.safety_warning);
    const safetyGuidance = hasPreviousSafety ? null : detectSafetyGuidance(text, {
      activeRepair: Boolean(repairWorkflow),
      navigation: repairAction?.action === 'done',
    });
    let assistantAdded = false;

    setLoading(true);
    sending.current = true;
    setError("");

    try {
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
          safetyGuidance,
          displayAfter: answerStartedAt + ANSWER_DISPLAY_DELAY_MS,
        },
      ]);
      assistantAdded = true;

      if (!repairAction) {
        setQuestion("");
        setImage(null);
      } else if (typedDone || repairAction?.action === 'report_issue') {
        setQuestion("");
      }

      const current = await ensureSession();

      if (epoch !== generation.current) {
        return;
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

      const remainingDisplayDelay = ANSWER_DISPLAY_DELAY_MS - (performance.now() - answerStartedAt);
      if (remainingDisplayDelay > 0) {
        await new Promise(resolve => setTimeout(resolve, remainingDisplayDelay));
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

      if (err.status === 410) {
        // The backend may have restarted before the browser's expiry time.
        // Forget the in-memory ID as well as the service's stored ID. Never
        // replay a repair confirmation against a newly created workflow.
        sessionRef.current = null;
        setSession(null);
        setRepairWorkflow(null);
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
