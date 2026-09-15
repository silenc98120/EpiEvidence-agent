import { useState, useRef, useEffect } from "react";

const API = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";
const CHAT_HISTORY: any[] = [
  {
    id: "1",
    title: "Typo Assistance Request",
    preview: "Can you help me fix typos in my document...",
    messages: [
      { role: "user", content: "Can you help me fix typos in my document? Here's the text: 'Ths is a sampl text with som errros in it.'" },
      { role: "assistant", content: "Of course! Here's the corrected version:\n\n\"This is a sample text with some errors in it.\"\n\nI fixed: 'Ths' → 'This', 'sampl' → 'sample', 'som' → 'some', 'errros' → 'errors'." },
    ],
  },
  {
    id: "2",
    title: "Quadratic Function Plot",
    preview: "How do I plot a quadratic function in Python?",
    messages: [
      { role: "user", content: "How do I plot a quadratic function in Python?" },
      { role: "assistant", content: "You can use matplotlib to plot a quadratic function:\n\n```python\nimport numpy as np\nimport matplotlib.pyplot as plt\n\nx = np.linspace(-10, 10, 300)\ny = x**2 + 2*x + 1  # y = (x+1)^2\n\nplt.plot(x, y)\nplt.title('Quadratic Function')\nplt.xlabel('x')\nplt.ylabel('y')\nplt.grid(True)\nplt.show()\n```\n\nThis plots y = x² + 2x + 1 over the range [-10, 10]." },
    ],
  },
  {
    id: "3",
    title: "Toyota Names Poetry",
    preview: "Write a poem using Toyota car model names.",
    messages: [
      { role: "user", content: "Write a poem using Toyota car model names." },
      { role: "assistant", content: "In the land of the **Corolla** and **Crown**,\nWhere the **Camry** rolls gracefully down,\nThe **Prius** hums softly at dawn,\nAnd the **RAV4** roams on and on.\n\nThe **Supra** screams past in the night,\nThe **Sequoia** stands tall in the light,\nFrom **Tacoma** to **Tundra** we ride,\nWith **Highlander** spirit and pride." },
    ],
  },
  {
    id: "4",
    title: "Urban Green Spaces",
    preview: "What are the benefits of urban green spaces?",
    messages: [
      { role: "user", content: "What are the benefits of urban green spaces?" },
      { role: "assistant", content: "Urban green spaces offer numerous benefits:\n\n**Environmental**\n- Reduce urban heat island effect\n- Improve air quality by absorbing CO₂\n- Support urban biodiversity\n- Manage stormwater runoff\n\n**Social & Health**\n- Reduce stress and improve mental health\n- Encourage physical activity\n- Foster community interaction\n- Provide children with nature exposure\n\n**Economic**\n- Increase nearby property values\n- Reduce energy costs through shade\n- Lower healthcare costs through preventive health benefits\n\nCities like Singapore and Copenhagen are leading examples of integrating green infrastructure at scale." },
    ],
  },
  {
    id: "5",
    title: "Historical Landmarks Guide",
    preview: "List the top 5 historical landmarks in Europe.",
    messages: [
      { role: "user", content: "List the top 5 historical landmarks in Europe." },
      { role: "assistant", content: "Here are 5 iconic historical landmarks in Europe:\n\n1. **The Colosseum, Rome** — Built in 70–80 AD, this ancient amphitheatre held up to 80,000 spectators.\n\n2. **The Acropolis, Athens** — Home to the Parthenon, a 5th-century BC temple dedicated to Athena.\n\n3. **Stonehenge, England** — A prehistoric monument dating to around 3000 BC, its purpose remains debated.\n\n4. **Versailles Palace, France** — The opulent royal château built by Louis XIV, a symbol of absolute monarchy.\n\n5. **Sagrada Família, Barcelona** — Gaudí's unfinished masterpiece, under construction since 1882." },
    ],
  },
  {
    id: "6",
    title: "Gourmet Food Truck Trends",
    preview: "What are the latest trends in gourmet food trucks?",
    messages: [
      { role: "user", content: "What are the latest trends in gourmet food trucks?" },
      { role: "assistant", content: "Gourmet food trucks in 2024–2025 are evolving fast:\n\n**Cuisine trends**\n- Korean-Mexican fusion (birria tacos, bulgogi burritos)\n- Elevated comfort food (truffle mac & cheese, lobster grilled cheese)\n- Plant-based fine dining on wheels\n\n**Tech & Operations**\n- Mobile ordering apps and QR menus\n- Ghost kitchen partnerships\n- AI-driven inventory and demand forecasting\n\n**Sustainability**\n- Compostable packaging and zero-waste kitchens\n- Locally sourced, seasonal menus\n- Solar-powered trucks\n\nCities like Portland, Austin, and Los Angeles remain food truck hotspots." },
    ],
  },
  {
    id: "7",
    title: "Digital Art Techniques",
    preview: "What are essential techniques for digital painting?",
    messages: [
      { role: "user", content: "What are essential techniques for digital painting?" },
      { role: "assistant", content: "Key techniques for digital painting:\n\n**Fundamentals**\n- **Layering** — Use separate layers for sketch, linework, flat color, shading, and highlights\n- **Brush control** — Vary pressure, opacity, and flow for natural-looking strokes\n- **Color blocking** — Establish flat base colors before adding detail\n\n**Lighting & Shadow**\n- Identify a single light source and stay consistent\n- Use multiply layers for shadows, screen/overlay for highlights\n- Study color temperature: warm lights, cool shadows\n\n**Tools to learn**\n- Procreate (iPad), Clip Studio Paint, or Photoshop\n- Invest in a Wacom or Apple Pencil for pressure sensitivity\n\nPractice studies from life: portraits, still-lifes, and landscapes." },
    ],
  },
  {
    id: "8",
    title: "Virtual Reality Experiences",
    preview: "How is VR being used in education today?",
    messages: [
      { role: "user", content: "How is VR being used in education today?" },
      { role: "assistant", content: "VR is transforming education in several meaningful ways:\n\n**Immersive Learning**\n- Virtual field trips to historical sites, space, or the ocean floor\n- 3D anatomy models for medical students\n- Simulated lab experiments without physical materials\n\n**Skills Training**\n- Surgical simulation for medical trainees\n- Flight simulators for pilot training\n- Soft skills practice (public speaking, job interviews)\n\n**Special Education**\n- Social skills training for students with autism\n- Safe environments to practice real-world scenarios\n\n**Platforms leading the way:** Google Expeditions (legacy), Labster, zSpace, and Meta's education initiatives.\n\nAdoption is still limited by cost and hardware availability, but declining headset prices are accelerating uptake." },
    ],
  },
];

type Message = { role: "user" | "assistant"; content: string };

function formatMessage(content: string) {
  const lines = content.split("\n");
  return lines.map((line, i) => {
    // Code block (single backtick inline code)
    const parts = line.split(/(`[^`]+`)/g);
    const formatted = parts.map((part, j) => {
      if (part.startsWith("`") && part.endsWith("`")) {
        return <code key={j} className="px-1.5 py-0.5 bg-gray-100 rounded text-sm font-mono">{part.slice(1, -1)}</code>;
      }
      // Bold **text**
      const boldParts = part.split(/(\*\*[^*]+\*\*)/g);
      return boldParts.map((bp, k) => {
        if (bp.startsWith("**") && bp.endsWith("**")) {
          return <strong key={k}>{bp.slice(2, -2)}</strong>;
        }
        return bp;
      });
    });
    return <p key={i} className={line === "" ? "mt-2" : ""}>{formatted}</p>;
  });
}

export default function App() {
  const [chats, setChats] = useState<any[]>([]);
  const [menuChat, setMenuChat] = useState<string|null>(null);
  const [editingChat, setEditingChat] = useState<string|null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [activeChat, setActiveChat] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const isNewChat = useRef(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const currentChat = chats.find((c) => c.conversation_id === activeChat);

  useEffect(() => { fetch(`${API}/api/conversations`).then(r=>r.json()).then(d=>setChats(d.items||[])).catch(()=>{}); }, []);

  useEffect(() => {
    if (isNewChat.current) {
      isNewChat.current = false;
      return;
    }
    if (activeChat) {
      fetch(`${API}/api/conversations/${activeChat}`).then(r=>r.json()).then(c=>setMessages((c.messages||[]).map((m:any)=>({role:m.role,content:m.content})))).catch(()=>setMessages([]));
    } else {
      setMessages([]);
    }
  }, [activeChat]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isTyping]);

  function handleNewChat() {
    setActiveChat(null);
    setMessages([]);
    setInput("");
  }

  async function renameChat(id:string){ const title=editTitle.trim(); if(!title) return; await fetch(`${API}/api/conversations/${id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({title})}); setChats(p=>p.map(c=>c.conversation_id===id?{...c,title}:c)); setEditingChat(null); setMenuChat(null); }
  async function deleteChat(id:string){ if(!confirm("确定删除这条聊天记录吗？")) return; await fetch(`${API}/api/conversations/${id}`,{method:"DELETE"}); setChats(p=>p.filter(c=>c.conversation_id!==id)); if(activeChat===id) handleNewChat(); setMenuChat(null); }

  async function handleSend() {
    const text = input.trim();
    if (!text) return;

    const userMsg: Message = { role: "user", content: text };
    const nextMessages = [...messages, userMsg];
    setMessages(nextMessages);
    setInput("");
    setIsTyping(true);

    let cid = activeChat;
    if (!cid) { const c = await fetch(`${API}/api/conversations`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:text.slice(0,40)})}).then(r=>r.json()); cid=c.conversation_id; setActiveChat(cid); setChats(p=>[c,...p]); }
    const task = await fetch(`${API}/api/tasks`, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:text,conversation_id:cid})}).then(r=>r.json());
    const res = await fetch(`${API}${task.events_url}`); const reader=res.body?.getReader(); if(!reader) return;
    const dec=new TextDecoder(); let buf=""; let assistant="";
    while(true){ const {done,value}=await reader.read(); if(done) break; buf+=dec.decode(value,{stream:true}); const chunks=buf.split("\n\n"); buf=chunks.pop()||""; for(const ch of chunks){ const dm=ch.match(/data: (.*)/); if(!dm) continue; const ev=JSON.parse(dm[1]); if(ev.type==="TEXT_MESSAGE_CONTENT") { assistant+=ev.delta||""; setMessages(p=>{const a=[...p]; const last=a[a.length-1]; if(last?.role==="assistant") a[a.length-1]={...last,content:assistant}; else a.push({role:"assistant",content:assistant}); return a;}); } if(ev.type==="RUN_FINISHED"||ev.type==="RUN_ERROR") setIsTyping(false); }}
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    e.stopPropagation();
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handleTextareaInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setInput(e.target.value);
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 160) + "px";
    }
  }

  const hasConversation = messages.length > 0;

  return (
    <div className="wallpaper flex h-screen font-['Inter',sans-serif] text-[#1c1c1e] overflow-hidden">

      {/* Sidebar — liquid glass panel */}
      <aside className="glass w-[256px] flex-shrink-0 flex flex-col shadow-[1px_0_0_rgba(255,255,255,0.6),4px_0_24px_rgba(0,0,0,0.06)]">
        {/* New chat button */}
        <div className="px-3 pt-4 pb-2">
          <button
            onClick={handleNewChat}
            className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-2xl hover:bg-white/50 active:bg-white/70 transition-all text-left"
          >
            <div className="w-6 h-6 rounded-lg bg-gradient-to-br from-[#5856D6] to-[#007AFF] flex items-center justify-center flex-shrink-0 shadow-sm">
              <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
                <path d="M5.5 2V9M2 5.5H9" stroke="white" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
            </div>
            <span className="text-[13px] font-medium text-[#1c1c1e]">新建对话</span>
          </button>
        </div>

        {/* Chat list */}
        <div className="flex-1 overflow-y-auto px-3 pb-3 sidebar-scroll">
          <p className="text-[10px] font-semibold text-[#1c1c1e]/35 uppercase tracking-widest px-3 py-2 mt-1">最近对话</p>
          <div className="flex flex-col gap-0.5">
            {chats.map((chat) => (
              <button
                key={chat.conversation_id}
                onClick={() => setActiveChat(chat.conversation_id)}
                className={`relative w-full text-left px-3 py-2.5 rounded-2xl transition-all duration-150 ${
                  activeChat === chat.conversation_id
                    ? "bg-white/60 shadow-[0_1px_8px_rgba(0,0,0,0.08)] backdrop-blur-sm"
                    : "hover:bg-white/35"
                }`}
              >
                {editingChat===chat.conversation_id ? <input autoFocus value={editTitle} onChange={e=>setEditTitle(e.target.value)} onKeyDown={e=>{if(e.key==='Enter') renameChat(chat.conversation_id); if(e.key==='Escape') setEditingChat(null)}} onBlur={()=>renameChat(chat.conversation_id)} className="w-full bg-white/70 rounded px-1 text-xs outline-none" /> : <p className="text-xs font-medium text-[#1c1c1e] truncate leading-snug">{chat.title}</p>}
                <p className="text-[11px] text-[#1c1c1e]/40 truncate mt-0.5 leading-snug">{chat.preview}</p>
                <span onClick={(e)=>{e.stopPropagation();setMenuChat(menuChat===chat.conversation_id?null:chat.conversation_id)}} className="absolute right-2 top-2 text-sm opacity-50 hover:opacity-100">⋯</span>
                {menuChat===chat.conversation_id && <div className="absolute right-2 top-8 z-20 glass rounded-xl p-1 shadow-lg"><button className="block px-3 py-1 text-xs hover:bg-white/60 rounded" onClick={(e)=>{e.stopPropagation();setEditingChat(chat.conversation_id);setEditTitle(chat.title);}}>重命名</button><button className="block px-3 py-1 text-xs text-red-500 hover:bg-white/60 rounded" onClick={(e)=>{e.stopPropagation();deleteChat(chat.conversation_id);}}>删除</button></div>}
              </button>
            ))}
          </div>
        </div>

        {/* Sidebar footer */}
        <div className="px-3 pb-4 pt-3 border-t border-white/40">
          <div className="flex items-center gap-3 px-3 py-2.5 rounded-2xl hover:bg-white/40 cursor-pointer transition-all">
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-[#5856D6] to-[#007AFF] flex items-center justify-center text-white text-xs font-semibold flex-shrink-0 shadow-sm">
              U
            </div>
            <p className="text-xs font-medium text-[#1c1c1e] truncate">用户账户</p>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 flex flex-col min-w-0">

        {/* Top bar — glass strip */}
        {(currentChat) && (
          <header className="glass border-b border-white/50 shadow-[0_1px_0_rgba(255,255,255,0.7)] flex items-center px-6 h-[52px] flex-shrink-0">
            <span className="text-[13px] font-semibold text-[#1c1c1e] truncate">
              {currentChat.title}
            </span>
          </header>
        )}

        {/* Messages area */}
        <div className="flex-1 overflow-y-auto">
          {!hasConversation ? (
            /* Welcome screen */
            <div className="flex flex-col items-center justify-center h-full px-6 pb-20 gap-0">
              {/* Glyph orb */}
              <div className="w-16 h-16 rounded-[22px] bg-gradient-to-br from-[#5856D6] via-[#AF52DE] to-[#007AFF] flex items-center justify-center mb-7 shadow-[0_8px_32px_rgba(88,86,214,0.35)]">
                <svg width="30" height="30" viewBox="0 0 30 30" fill="none">
                  <path d="M15 4L18.5 12.5H27L20.5 17.5L23 26L15 21L7 26L9.5 17.5L3 12.5H11.5L15 4Z" fill="white" fillOpacity="0.95"/>
                </svg>
              </div>

              <h1 className="text-[28px] font-semibold text-[#1c1c1e] mb-2 text-center tracking-[-0.5px] leading-tight">
                今天有什么可以帮助你的？
              </h1>
              <p className="text-[14px] text-[#1c1c1e]/45 text-center max-w-xs leading-relaxed">
                向我提问任何问题，或从左侧选择历史对话
              </p>

              {/* Glass suggestion chips */}
              <div className="flex flex-wrap gap-2 mt-8 justify-center max-w-sm">
                {["解释量子计算", "制定学习计划", "分析数据趋势"].map((s) => (
                  <button
                    key={s}
                    onClick={() => setInput(s)}
                    className="glass px-4 py-2 rounded-full text-[13px] text-[#1c1c1e]/75 hover:text-[#1c1c1e] hover:bg-white/75 shadow-[0_1px_6px_rgba(0,0,0,0.07)] transition-all active:scale-95"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            /* Chat messages */
            <div className="max-w-[640px] mx-auto px-5 py-6 space-y-5">
              {messages.map((msg, i) => (
                <div key={i} className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : "items-end"}`}>
                  {msg.role === "assistant" && (
                    <div className="w-7 h-7 rounded-full bg-gradient-to-br from-[#5856D6] via-[#AF52DE] to-[#007AFF] flex items-center justify-center flex-shrink-0 shadow-[0_2px_8px_rgba(88,86,214,0.3)]">
                      <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                        <path d="M6 1.5L7.8 5.2H11.5L8.5 7.4L9.7 11L6 8.8L2.3 11L3.5 7.4L0.5 5.2H4.2L6 1.5Z" fill="white"/>
                      </svg>
                    </div>
                  )}
                  <div
                    className={`max-w-[82%] text-[14px] leading-relaxed ${
                      msg.role === "user"
                        ? "bg-[#007AFF] text-white px-4 py-2.5 rounded-[20px] rounded-br-[6px] shadow-[0_2px_12px_rgba(0,122,255,0.30)]"
                        : "glass text-[#1c1c1e] px-4 py-3 rounded-[20px] rounded-bl-[6px] shadow-[0_2px_12px_rgba(0,0,0,0.07)]"
                    }`}
                  >
                    {msg.role === "assistant" ? (
                      <div className="space-y-1">{formatMessage(msg.content)}</div>
                    ) : (
                      msg.content
                    )}
                  </div>
                </div>
              ))}

              {isTyping && (
                <div className="flex gap-3 items-end">
                  <div className="w-7 h-7 rounded-full bg-gradient-to-br from-[#5856D6] via-[#AF52DE] to-[#007AFF] flex items-center justify-center flex-shrink-0 shadow-[0_2px_8px_rgba(88,86,214,0.3)]">
                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                      <path d="M6 1.5L7.8 5.2H11.5L8.5 7.4L9.7 11L6 8.8L2.3 11L3.5 7.4L0.5 5.2H4.2L6 1.5Z" fill="white"/>
                    </svg>
                  </div>
                  <div className="glass px-4 py-3.5 rounded-[20px] rounded-bl-[6px] shadow-[0_2px_12px_rgba(0,0,0,0.07)] flex items-center gap-1.5">
                    {[0, 1, 2].map((i) => (
                      <div
                        key={i}
                        className="w-1.5 h-1.5 bg-[#1c1c1e]/30 rounded-full animate-bounce"
                        style={{ animationDelay: `${i * 0.18}s` }}
                      />
                    ))}
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        {/* Input bar — floating glass pill */}
        <div className="px-5 pb-5 pt-2 flex-shrink-0">
          <div className="max-w-[640px] mx-auto">
            <div className="glass flex items-end gap-2 rounded-[22px] px-4 py-3 shadow-[0_4px_24px_rgba(0,0,0,0.10),0_1px_0_rgba(255,255,255,0.8)_inset] focus-within:shadow-[0_4px_28px_rgba(0,0,0,0.14),0_1px_0_rgba(255,255,255,0.8)_inset] transition-shadow">
              <textarea
                ref={textareaRef}
                value={input}
                onChange={handleTextareaInput}
                onKeyDown={handleKeyDown}
                onKeyUp={(e) => e.stopPropagation()}
                onKeyPress={(e) => e.stopPropagation()}
                placeholder="发送消息…"
                rows={1}
                className="flex-1 resize-none bg-transparent text-[14px] text-[#1c1c1e] placeholder-[#1c1c1e]/30 outline-none leading-relaxed min-h-[24px] max-h-[160px] py-0"
                style={{ height: "24px" }}
              />

              {/* Voice button */}
              <button
                className="w-8 h-8 rounded-full bg-white/50 hover:bg-white/80 active:scale-90 transition-all flex items-center justify-center flex-shrink-0 shadow-[0_1px_4px_rgba(0,0,0,0.08)]"
                title="语音输入（即将上线）"
              >
                <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
                  <rect x="4.5" y="0.5" width="4" height="6.5" rx="2" fill="#1c1c1e" fillOpacity="0.45"/>
                  <path d="M2 6C2 8.76142 4.01472 11 6.5 11C8.98528 11 11 8.76142 11 6" stroke="#1c1c1e" strokeOpacity="0.45" strokeWidth="1.2" strokeLinecap="round"/>
                  <path d="M6.5 11V12.5" stroke="#1c1c1e" strokeOpacity="0.45" strokeWidth="1.2" strokeLinecap="round"/>
                </svg>
              </button>

              {/* Send button */}
              <button
                onClick={handleSend}
                disabled={!input.trim()}
                className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 transition-all active:scale-90 ${
                  input.trim()
                    ? "bg-[#007AFF] text-white shadow-[0_2px_10px_rgba(0,122,255,0.40)] hover:brightness-110"
                    : "bg-white/40 text-[#1c1c1e]/20 cursor-not-allowed"
                }`}
              >
                <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
                  <path d="M6.5 10.5V2.5M3 6L6.5 2.5L10 6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
            </div>
            <p className="text-center text-[11px] text-[#1c1c1e]/25 mt-2">
              AI 助手可能会犯错，请核实重要信息
            </p>
          </div>
        </div>
      </main>
    </div>
  );
}
