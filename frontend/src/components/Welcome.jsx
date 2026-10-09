import GearLogo from "./GearLogo";
import { APP_CONFIG } from "../config/appConfig";

// children = the ChatInput, so it sits between the heading and the examples
function Welcome({ children, onExampleSelect }) {
  return (
    <div className="welcome">
      <div className="logo">
        <GearLogo size={90} />
      </div>

      <div className="heading">
        <h1>
          Manufacturing Equipment <span>Copilot</span>
        </h1>
        <p>Upload an image and ask your question</p>
      </div>

      <div className="welcome-input">{children}</div>

      <div className="examples">
        <div className="examples-title">
          <span>Try these examples</span>
        </div>

        <div className="example-buttons">
          {APP_CONFIG.exampleQuestions.map((text) => (
            <button
              type="button"
              key={text}
              onClick={() => onExampleSelect(text)}
            >
              {text}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export default Welcome;