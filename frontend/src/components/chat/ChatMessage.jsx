import { useEffect, useState } from "react";
import ResponseRenderer from "./ResponseRenderer";

function LoadingAssistant({ message }) {
  return <>
    {message.safetyGuidance && <div className="preparation-safety" role="status">
      <strong>{message.safetyGuidance.title}</strong>
      <ul>{message.safetyGuidance.bullets.map(bullet => <li key={bullet}>{bullet}</li>)}</ul>
    </div>}
    <p className="preparing-answer">Preparing troubleshooting steps…</p>
    <div className="typing" aria-label="Analyzing"><span></span><span></span><span></span></div>
  </>;
}

function ChatMessage({ message, loading, repairActive, onRepairAction, showSafetyWarning }) {
  const [loadingVisible, setLoadingVisible] = useState(() =>
    message.status !== "loading" || !message.displayAfter || performance.now() >= message.displayAfter
  );

  useEffect(() => {
    if (message.status !== "loading" || loadingVisible || !message.displayAfter) return undefined;
    const delay = Math.max(0, message.displayAfter - performance.now());
    const timer = setTimeout(() => setLoadingVisible(true), delay);
    return () => clearTimeout(timer);
  }, [message.displayAfter, message.status, loadingVisible]);

  if (message.role === "user") {
    return (
      <div className="message message--user">
        <div className="user-message">
          {message.image && <img src={message.image} alt="Uploaded component" />}
          {message.text && <p>{message.text}</p>}
        </div>
      </div>
    );
  }

  if (message.status === "loading" && !loadingVisible) return null;

  return (
    <div className="message message--assistant">
      <div className="assistant-message">
        {message.status === "loading" && (
          <LoadingAssistant message={message} />
        )}

        {message.status === "error" && (
          <p className="error-text" role="alert">{message.error}</p>
        )}

        {message.status === "done" && (
          <ResponseRenderer response={message.response} loading={loading}
            repairActive={repairActive} onRepairAction={onRepairAction}
            showSafetyWarning={showSafetyWarning} />
        )}
      </div>
    </div>
  );
}

export default ChatMessage;
