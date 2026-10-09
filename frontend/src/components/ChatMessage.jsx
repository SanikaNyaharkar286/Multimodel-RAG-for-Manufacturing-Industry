import ResponseRenderer from "./ResponseRenderer";

function ChatMessage({ message }) {
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

  return (
    <div className="message message--assistant">
      <div className="assistant-message">
        {message.status === "loading" && (
          <div className="typing" aria-label="Analyzing">
            <span></span>
            <span></span>
            <span></span>
          </div>
        )}

        {message.status === "error" && (
          <p className="error-text">{message.error}</p>
        )}

        {message.status === "done" && (
          <ResponseRenderer response={message.response} />
        )}
      </div>
    </div>
  );
}

export default ChatMessage;