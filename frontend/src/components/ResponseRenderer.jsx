import DiagramPlaceholder from "./DiagramPlaceholder";
import SourceCitation from "./SourceCitation";

function renderBlock(block, index) {
  switch (block.type) {
    case "text":
      return (
        <div className="response-text" key={index}>
          {block.content.split("\n\n").map((paragraph, i) => (
            <p key={i}>{paragraph}</p>
          ))}
        </div>
      );

    case "image":
    case "diagram":
      return (
        <figure className="response-figure" key={index}>
          {block.src ? (
            <img src={block.src} alt={block.caption || "Response image"} />
          ) : (
            <DiagramPlaceholder />
          )}
          {block.caption && <figcaption>{block.caption}</figcaption>}
        </figure>
      );

    case "table":
      return (
        <div className="response-table" key={index}>
          {block.title && <h3 className="response-title">{block.title}</h3>}
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  {block.columns.map((column) => (
                    <th key={column}>{column}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );

    case "steps":
      return (
        <div className="response-steps" key={index}>
          {block.title && <h3 className="response-title">{block.title}</h3>}
          <ol className="steps-list">
            {block.items.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ol>
        </div>
      );

    default:
      return null;
  }
}

function ResponseRenderer({ response }) {
  if (!response) return null;

  return (
    <div className="response">
      {response.demo && (
        <p className="demo-badge">
          Sample response (demo mode): not from your manuals
        </p>
      )}

      {response.blocks?.map((block, index) => renderBlock(block, index))}

      <SourceCitation sources={response.sources} />
    </div>
  );
}

export default ResponseRenderer;