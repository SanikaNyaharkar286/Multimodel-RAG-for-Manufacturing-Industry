import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import "../../styles/agent-response.css";


function safeLink(value) {
  try {
    const url = new URL(value);

    return ["http:", "https:"].includes(url.protocol)
      ? url.href
      : null;
  } catch {
    return null;
  }
}


function MarkdownTable({ children }) {
  return (
    <div className="table-wrapper">
      <table>{children}</table>
    </div>
  );
}


function renderBlock(block, index) {
  switch (block.type) {
    case "text":
      return (
        <div
          className="response-text markdown-response"
          key={index}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            skipHtml
            disallowedElements={["img"]}
            components={{
              table: MarkdownTable,
            }}
          >
            {block.content || ""}
          </ReactMarkdown>
        </div>
      );

    case "image":
      return (
        <figure
          className="response-figure"
          key={index}
        >
          <img
            src={block.src}
            alt={
              block.caption ||
              "Original manual diagram"
            }
            loading="lazy"
            onError={(event) => {
              event.currentTarget.style.display = "none";

              const note =
                event.currentTarget.nextElementSibling;

              if (note) {
                note.hidden = false;
              }
            }}
          />

          <p hidden>
            Image unavailable or session expired.
          </p>

          <figcaption>
            {block.caption && <strong>{block.caption}</strong>}
            {block.description && (
              <div className="image-description">
                <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml disallowedElements={["img"]}>
                  {block.description}
                </ReactMarkdown>
              </div>
            )}
          </figcaption>
        </figure>
      );

    case "table":
      return (
        <div
          className="response-table"
          key={index}
        >
          {block.title && (
            <h3>{block.title}</h3>
          )}

          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  {(block.columns || []).map(
                    (column, columnIndex) => (
                      <th key={columnIndex}>
                        {column}
                      </th>
                    )
                  )}
                </tr>
              </thead>

              <tbody>
                {(block.rows || []).map(
                  (row, rowIndex) => (
                    <tr key={rowIndex}>
                      {row.map(
                        (cell, cellIndex) => (
                          <td key={cellIndex}>
                            {cell}
                          </td>
                        )
                      )}
                    </tr>
                  )
                )}
              </tbody>
            </table>
          </div>
        </div>
      );

    case "link": {
      const url = safeLink(block.url);

      if (!url) return null;

      return (
        <p
          className="response-video-link"
          key={index}
        >
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {block.title ||
              (block.isVideo
                ? "Watch related video"
                : "Open manual reference")}
          </a>
        </p>
      );
    }

    default:
      return null;
  }
}


export default function ResponseRenderer({
  response,
}) {
  if (!response) return null;

  return (
    <div className="response agent-response">
      {(response.blocks || []).map(renderBlock)}

      {response.asset_warnings?.length > 0 && (
        <p className="reference-notice">
          Some original reference media could not be loaded.
        </p>
      )}
    </div>
  );
}
