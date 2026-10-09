function DiagramPlaceholder() {
  return (
    <div className="diagram-placeholder">
      <svg
        width="44"
        height="44"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="8" cy="8" r="1.5" />
        <path d="m21 15-5-5L5 21" />
      </svg>
      <span>Manual diagram will appear here</span>
    </div>
  );
}

export default DiagramPlaceholder;