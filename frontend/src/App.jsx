import "./styles/global.css";
import "./styles/chat.css";

import useChat from "./hooks/useChat";
import Header from "./components/Header";
import Welcome from "./components/Welcome";
import ChatInput from "./components/ChatInput";
import ChatThread from "./components/ChatThread";

function App() {
  const chat = useChat();
  const hasMessages = chat.messages.length > 0;

  const input = (
    <ChatInput
      question={chat.question}
      setQuestion={chat.setQuestion}
      image={chat.image}
      error={chat.error}
      loading={chat.loading}
      compact={hasMessages}
      onImageSelect={chat.selectImage}
      onImageRemove={chat.removeImage}
      onSend={chat.sendMessage}
    />
  );

  return (
    <div className={`app ${hasMessages ? "app--chat" : ""}`}>
      {hasMessages ? (
        <div className="chat-layout">
          <Header onNewChat={chat.resetChat} />
          <ChatThread messages={chat.messages} />
          <div className="input-dock">
            <div className="dock-inner">{input}</div>
          </div>
        </div>
      ) : (
        <main className="main-container">
          <Welcome onExampleSelect={chat.setQuestion}>{input}</Welcome>
        </main>
      )}
    </div>
  );
}

export default App;