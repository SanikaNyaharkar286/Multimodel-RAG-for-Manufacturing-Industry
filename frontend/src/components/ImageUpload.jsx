import { APP_CONFIG } from "../config/appConfig";

function ImageUpload({ onSelect, disabled }) {
  function handleChange(event) {
    const file = event.target.files[0];
    if (file) onSelect(file);
    event.target.value = ""; // allows picking the same file again
  }

  return (
    <label className="upload-btn" title="Upload image">
      <input
        type="file"
        accept={APP_CONFIG.allowedImageTypes.join(",")}
        onChange={handleChange}
        disabled={disabled}
        hidden
      />

      <svg
        width="30"
        height="30"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="8" cy="8" r="1.5" />
        <path d="m21 15-5-5L5 21" />
      </svg>
    </label>
  );
}

export default ImageUpload;