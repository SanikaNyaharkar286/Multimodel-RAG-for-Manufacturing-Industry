
import { useState } from "react";
import { sendChat } from "../services/chatService";
import { validateImage } from "../utils/validateImage";

// Create unique message IDs
const makeId = () =>
  `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

// Create unique ADK session ID
const makeSessionId = () =>
  `session-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

function useChat() {
  const [question, setQuestion] = useState("");

  const [image, setImage] = useState(null);

  const [messages, setMessages] = useState([]);

  const [loading, setLoading] = useState(false);

  const [error, setError] = useState("");

  const [sessionId, setSessionId] = useState(makeSessionId);

  // Select image
  function selectImage(file) {
    const result = validateImage(file);

    if (!result.valid) {
      setError(result.error);
      return;
    }

    setError("");

    if (image) {
      URL.revokeObjectURL(image.previewUrl);
    }

    setImage({
      file,
      previewUrl: URL.createObjectURL(file),
    });
  }

  // Remove selected image
  function removeImage() {
    if (image) {
      URL.revokeObjectURL(image.previewUrl);
    }

    setImage(null);
  }

  // Send message to backend
  async function sendMessage(event) {
    if (event) {
      event.preventDefault();
    }

    const text = question.trim();

    if ((!text && !image) || loading) {
      return;
    }

    const assistantId = makeId();

    const imageFile = image ? image.file : null;

    const userMessage = {
      id: makeId(),
      role: "user",
      text,
      image: image ? image.previewUrl : null,
    };

    const assistantMessage = {
      id: assistantId,
      role: "assistant",
      status: "loading",
      response: null,
      error: "",
    };

    // Display user message and loading response
    setMessages((prev) => [
      ...prev,
      userMessage,
      assistantMessage,
    ]);

    setQuestion("");
    setImage(null);
    setError("");
    setLoading(true);

    try {
      // Call FastAPI through chatService
      const response = await sendChat({
        question: text,
        imageFile,
        sessionId,
      });

      // Display Gemini answer
      setMessages((prev) =>
        prev.map((message) =>
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
      // Display an error if request fails
      setMessages((prev) =>
        prev.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                status: "error",
                error:
                  err.message ||
                  "Something went wrong. Please try again.",
              }
            : message
        )
      );
    } finally {
      setLoading(false);
    }
  }

  // Clear conversation and create a new session
  function resetChat() {
    messages.forEach((message) => {
      if (message.image) {
        URL.revokeObjectURL(message.image);
      }
    });

    if (image) {
      URL.revokeObjectURL(image.previewUrl);
    }

    setMessages([]);
    setImage(null);
    setQuestion("");
    setError("");
    setSessionId(makeSessionId());
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
  };
}

export default useChat;
