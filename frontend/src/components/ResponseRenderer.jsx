function isVideo(url) {
  const host = new URL(url).hostname.toLowerCase();

  return (
    host === "youtu.be" ||
    host === "youtube.com" ||
    host.endsWith(".youtube.com")
  );
}

function renderBlock(block, index) {
  switch (block.type) {
    case "text":
      return (
        <div className="response-text" key={index}>
          {(block.content || "").split("\n\n").map((paragraph, i) => (
            <p key={i} style={{ whiteSpace: "pre-line" }}>
              {paragraph}
            </p>
          ))}
        </div>
      );

    case "image":
      return (
        <div className="manual-image-section" key={index}>
          <h3>Original Manual Image</h3>

          <figure className="response-figure">
            <img
              src={block.src}
              alt="Manual diagram"
              loading="lazy"
              style={{ maxWidth: "100%", height: "auto", borderRadius: "8px" }}
              onError={(event) => {
                event.currentTarget.style.display = "none";
                const note = event.currentTarget.nextElementSibling;
                if (note) note.hidden = false;
              }}
            />
            <p hidden>Original image unavailable.</p>
          </figure>

          <div className="image-explanation">
            <h3>Detailed Image Explanation</h3>
            <p style={{ whiteSpace: "pre-line" }}>
              {block.caption || "No explanation available from the manual."}
            </p>
          </div>
        </div>
      );

    case "table":
      return (
        <div className="response-table" key={index}>
          <h3 className="response-title">{block.title}</h3>

          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  {block.columns.map((column, i) => (
                    <th key={i}>{column}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );

    case "link":
      return (
        <p className="response-text" key={index}>
          <a href={block.url} target="_blank" rel="noopener noreferrer">
            {isVideo(block.url)
              ? "Watch related video"
              : "Open related manual link"}
          </a>
        </p>
      );

    default:
      return null;
  }
}

function ResponseRenderer({ response }) {
  if (!response) return null;

  return (
    <div className="response">
      {response.blocks.map((block, index) => renderBlock(block, index))}
    </div>
  );
}

export default ResponseRenderer;