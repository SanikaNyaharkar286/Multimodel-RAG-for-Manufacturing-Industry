import "./styles/global.css";
import "./styles/chat.css";

import useChat from "./hooks/useChat";
import Header from "./components/layout/Header";
import Welcome from "./components/layout/Welcome";
import ChatInput from "./components/chat/ChatInput";
import ChatThread from "./components/chat/ChatThread";
import { AuthGate } from "./auth/AuthGate";

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

  return <AuthGate><>
    {(
    <div className={`app ${hasMessages ? "app--chat" : ""}`}>
      {hasMessages ? (
        <div className="chat-layout">
          <Header onNewChat={chat.resetChat} />
          <ChatThread messages={chat.messages} loading={chat.loading}
            repairWorkflow={chat.repairWorkflow} onRepairAction={chat.sendRepairAction} />
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
    )}
  </></AuthGate>;
}

export default App;
