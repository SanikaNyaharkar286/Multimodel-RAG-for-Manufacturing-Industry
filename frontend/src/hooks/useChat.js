import { useState } from "react";
import { sendChat } from "../services/chatService";
import { validateImage } from "../utils/validateImage";

const makeId = () =>
  `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

function useChat() {
  const [question, setQuestion] = useState("");
  const [image, setImage] = useState(null); // { file, previewUrl }
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  function selectImage(file) {
    const result = validateImage(file);

    if (!result.valid) {
      setError(result.error);
      return;
    }

    setError("");
    if (image) URL.revokeObjectURL(image.previewUrl);
    setImage({ file, previewUrl: URL.createObjectURL(file) });
  }

  function removeImage() {
    if (image) URL.revokeObjectURL(image.previewUrl);
    setImage(null);
  }

  async function sendMessage(event) {
    if (event) event.preventDefault();

    const text = question.trim();
    if ((!text && !image) || loading) return;

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

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setQuestion("");
    setImage(null);
    setError("");
    setLoading(true);

    try {
      const response = await sendChat({ question: text, imageFile });

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, status: "done", response } : m
        )
      );
    } catch (err) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                status: "error",
                error: err.message || "Something went wrong. Please try again.",
              }
            : m
        )
      );
    } finally {
      setLoading(false);
    }
  }

  function resetChat() {
    messages.forEach((m) => {
      if (m.image) URL.revokeObjectURL(m.image);
    });
    if (image) URL.revokeObjectURL(image.previewUrl);

    setMessages([]);
    setImage(null);
    setQuestion("");
    setError("");
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