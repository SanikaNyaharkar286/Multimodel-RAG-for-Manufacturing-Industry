import ResponseRenderer from "./ResponseRenderer";
import WorkspaceIcon from "../layout/WorkspaceIcon";
import { chatErrorMessage } from '../../utils/chatErrorMessage';

function LoadingAssistant({ message }) {
  return <>
    {message.safetyChecking && !message.safetyGuidance && <p className="preparing-answer">Checking relevant precautions…</p>}
    {message.safetyGuidance && <div className="preparation-safety" role="status">
      <strong>{message.safetyGuidance.title}</strong>
      <ul>{message.safetyGuidance.bullets.map(bullet => <li key={bullet}>{bullet}</li>)}</ul>
    </div>}
    <p className="preparing-answer">{message.stage || 'Preparing troubleshooting steps…'}</p>
    <div className="typing" aria-label="Analyzing"><span></span><span></span><span></span></div>
  </>;
}

function ChatMessage({ message, loading, repairActive, onRepairAction, showSafetyWarning,
  allowDocumentation, onRetry, onRefresh }) {
  if (message.status === 'done' && message.response?.case_conflict) return null;

  if (message.role === "user") {
    return (
      <div className="message message--user">
        <div className="user-message">
          <span className="message-author">You</span>
          {message.image && <img src={message.image} alt="Uploaded component" />}
          {message.text && <p>{message.text}</p>}
        </div>
      </div>
    );
  }


  return (
    <div className="message message--assistant">
      <div className="assistant-message">
        <div className="assistant-heading"><span className="assistant-symbol"><WorkspaceIcon name="activity" size={17} /></span><strong>Equipment Copilot</strong><span className="assistant-kind">{message.status === 'loading' ? 'Working on your question' : message.response?.repair_workflow?.current_step ? 'Guided troubleshooting' : 'Manual assistant'}</span></div>
        {message.status === "loading" && (
          <LoadingAssistant message={message} />
        )}

        {message.status === "error" && (
          <div className="request-error"><p className="error-text" role="alert">{chatErrorMessage(message.error, message.errorStatus)}</p>{message.errorStatus === 409 && onRefresh
            ? <button type="button" disabled={loading} onClick={onRefresh}>Refresh active step</button>
            : message.retry && onRetry && <button type="button" disabled={loading} onClick={() => onRetry(message)}>Retry request</button>}</div>
        )}

        {message.status === "done" && (
          <ResponseRenderer response={message.response} loading={loading}
            repairActive={repairActive} onRepairAction={onRepairAction}
            showSafetyWarning={showSafetyWarning}
            allowDocumentation={allowDocumentation} />
        )}
      </div>
    </div>
  );
}

export default ChatMessage;
