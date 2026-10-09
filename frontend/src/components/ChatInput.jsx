import ImageUpload from "./ImageUpload";

function ChatInput({
  question,
  setQuestion,
  image,
  error,
  loading,
  compact,
  onImageSelect,
  onImageRemove,
  onSend,
}) {
  return (
    <>
      <form
        className={`input-container ${compact ? "compact" : ""}`}
        onSubmit={onSend}
      >
        {image && (
          <div className="image-preview">
            <img src={image.previewUrl} alt="Selected machine component" />
            <button
              type="button"
              className="remove-image"
              onClick={onImageRemove}
              aria-label="Remove image"
            >
              ×
            </button>
          </div>
        )}

        <div className="question-box">
          <input
            type="text"
            placeholder="Ask anything about this image..."
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <ImageUpload onSelect={onImageSelect} disabled={loading} />
        </div>

        <button
          type="submit"
          className="send-btn"
          aria-label="Send"
          disabled={loading}
        >
          <svg
            width="30"
            height="30"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.3"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="m22 2-7 20-4-9-9-4Z" />
            <path d="M22 2 11 13" />
          </svg>
        </button>
      </form>

      {error && <p className="form-error">{error}</p>}
    </>
  );
}

export default ChatInput;