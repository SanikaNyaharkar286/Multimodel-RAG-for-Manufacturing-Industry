import WorkspaceIcon from "./WorkspaceIcon";

const EXAMPLES = [
  { icon: 'activity', title: 'Troubleshoot a fault', text: 'Blade vibration, pressure loss, or low cutting efficiency.',
    prompt: 'The band saw blade is experiencing significant vibration. What should I check?' },
  { icon: 'image', title: 'Find the right diagram', text: 'Locate a component and understand the manual.',
    prompt: 'My saw blade is shifting during operation. Show the relevant blade-guide or carbide-guide image and tell me how to fix it.' },
  { icon: 'wrench', title: 'Check a maintenance issue', text: 'Get a guided inspection, one step at a time.',
    prompt: 'Cutting efficiency is very low. Show the coolant tank or coolant-pump image and tell me how to fix this issue.' },
];

function Welcome({ children, onExampleSelect }) {
  return (
    <div className="welcome">
      <div className="workspace-eyebrow"><span /> YOUR MAINTENANCE WORKSPACE</div>
      <div className="heading">
        <h1>Let’s <span>troubleshoot.</span></h1>
        <p>Ask a question or upload a photo.</p>
      </div>

      <div className="welcome-input">{children}</div>
      <div className="welcome-workflow" aria-label="How the workspace works">
        <span><WorkspaceIcon name="image" size={16} /> Describe the issue</span><WorkspaceIcon name="arrow" size={14} />
        <span><WorkspaceIcon name="wrench" size={16} /> Work step by step</span><WorkspaceIcon name="arrow" size={14} />
        <span><WorkspaceIcon name="clipboard" size={16} /> Document the result</span>
      </div>
      <section className="examples" aria-labelledby="examples-title">
        <div className="examples-heading"><h2 id="examples-title">A good place to start</h2><span>Select a prompt, then make it yours</span></div>
        <div className="example-buttons">{EXAMPLES.map(example => <button type="button" key={example.title} onClick={() => {
          onExampleSelect?.(example.prompt);
          document.getElementById('chat-question')?.focus();
        }}>
          <span className="example-icon"><WorkspaceIcon name={example.icon} size={22} /></span>
          <strong>{example.title}</strong><p>{example.text}</p><span className="example-arrow"><WorkspaceIcon name="arrow" size={18} /></span>
        </button>)}</div>
      </section>
      <p className="welcome-footnote">Grounded in your equipment manuals. Built around your observations.</p>
    </div>
  );
}

export default Welcome;
