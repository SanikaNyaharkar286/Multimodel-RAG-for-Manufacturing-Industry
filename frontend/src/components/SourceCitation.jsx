function SourceCitation({ sources }) {
  if (!sources || sources.length === 0) return null;

  return (
    <div className="sources">
      <h3 className="sources-title">Sources</h3>

      <div className="source-list">
        {sources.map((source, index) => (
          <div className="source-chip" key={index}>
            <strong>{source.document}</strong>
            {source.page && <span> · Page {source.page}</span>}
            {source.section && <span> · {source.section}</span>}
          </div>
        ))}
      </div>
    </div>
  );
}

export default SourceCitation;