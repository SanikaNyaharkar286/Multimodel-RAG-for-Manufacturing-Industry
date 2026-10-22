import GearLogo from "./GearLogo";

function Welcome({ children }) {
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

    </div>
  );
}

export default Welcome;
