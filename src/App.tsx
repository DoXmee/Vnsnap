/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState, useRef, useEffect, ChangeEvent } from 'react';
import { 
  Languages, 
  FileText, 
  Upload, 
  Download, 
  ArrowRightLeft, 
  Loader2, 
  CheckCircle2, 
  AlertCircle, 
  Github, 
  History, 
  Trash2, 
  Play, 
  Pause,
  FileDown,
  Clock,
  Eye,
  Settings,
  AlertTriangle,
  XCircle,
  Wifi
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { translateLargeText, translateSubtitleFileContent, setApiKeys } from './services/gemini';
import { dbStore } from './services/db';

type Tab = 'text' | 'file' | 'history' | 'settings';

interface QueuedFile {
  id: string;
  file?: File;
  name: string;
  size: number;
  status: 'pending' | 'translating' | 'completed' | 'error' | 'interrupted';
  progress: number;
  translatedContent?: string;
  error?: string;
  rawContent?: string;
  translatedChunks?: string[];
  chunksCount?: number;
}

interface HistoryItem {
  id: string;
  name: string;
  size: number;
  type: 'text' | 'file';
  content: string;
  timestamp: number;
}

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>('text');
  const [inputText, setInputText] = useState(() => {
    try {
      return localStorage.getItem('linhhoat_input') || '';
    } catch (e) { return ''; }
  });
  const [translatedText, setTranslatedText] = useState(() => {
    try {
      return localStorage.getItem('linhhoat_translated') || '';
    } catch (e) { return ''; }
  });
  const [isTranslating, setIsTranslating] = useState(false);
  const [globalProgress, setGlobalProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [additionalKeys, setAdditionalKeys] = useState<string[]>([]);
  const [elapsedTime, setElapsedTime] = useState(0);
  const [logs, setLogs] = useState<{ time: string; msg: string; type: 'info' | 'error' }[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll logs
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs]);

  const addLog = (msg: string, type: 'info' | 'error' = 'info') => {
    const time = new Date().toLocaleTimeString();
    setLogs(prev => [...prev, { time, msg, type }].slice(-150)); // Giữ 150 log cuối
  };
  
  // History and Queue states
  const [fileQueue, setFileQueue] = useState<QueuedFile[]>([]);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const timerRef = useRef<NodeJS.Timeout | null>(null);
  const actualStartTimeRef = useRef<number | null>(null);
  const pausedFileIdsRef = useRef<Set<string>>(new Set());

  // Asynchronously restore state on mount from IndexedDB (or localStorage fallback)
  useEffect(() => {
    const loadAllSavedState = async () => {
      try {
        const input = await dbStore.get('linhhoat_input');
        if (input !== null) setInputText(input);

        const translated = await dbStore.get('linhhoat_translated');
        if (translated !== null) setTranslatedText(translated);

        const queue = await dbStore.get('linhhoat_queue');
        if (queue !== null) {
          let hasInterrupted = false;
          const processedQueue = queue.map((f: any) => {
            if (f.status === 'translating') {
              hasInterrupted = true;
              return { 
                ...f, 
                status: 'interrupted',
                error: 'Dịch bị gián đoạn'
              };
            }
            return f;
          });
          setFileQueue(processedQueue);
          if (hasInterrupted) {
            setTimeout(() => {
              addLog("Phát hiện tiến trình dịch tệp bị gián đoạn đột ngột (do tắt tab/mất kết nối).", "error");
              addLog("Bạn có thể bấm nút 'Dịch tiếp' (biểu tượng ▶ cạnh tệp tin) để khôi phục dịch từ cụm dở dang, tránh dịch lại từ đầu!", "info");
            }, 1000);
          }
        }

        const hist = await dbStore.get('linhhoat_history');
        if (hist !== null && Array.isArray(hist)) {
          setHistory(hist);
        }

        const savedLogs = await dbStore.get('linhhoat_logs');
        if (savedLogs !== null) setLogs(savedLogs);
      } catch (err) {
        console.error("Lỗi khi khôi phục trạng thái từ database:", err);
      }
    };
    
    loadAllSavedState();

    // Load API Keys
    let geminiKey = "";
    try {
      geminiKey = process.env.GEMINI_API_KEY || "";
    } catch (e) {}

    const savedKeys = localStorage.getItem('linhhoat_keys');
    if (savedKeys) {
      try {
        const keys = JSON.parse(savedKeys);
        setAdditionalKeys(keys);
        setApiKeys([geminiKey, ...keys].filter(k => !!k));
      } catch (e) { console.error(e); }
    } else if (geminiKey) {
      setApiKeys([geminiKey]);
    }
  }, []);

  // Debounced, non-blocking asynchronous state writers
  useEffect(() => {
    const timer = setTimeout(() => {
      dbStore.set('linhhoat_input', inputText);
    }, 600);
    return () => clearTimeout(timer);
  }, [inputText]);

  useEffect(() => {
    const timer = setTimeout(() => {
      dbStore.set('linhhoat_translated', translatedText);
    }, 600);
    return () => clearTimeout(timer);
  }, [translatedText]);

  useEffect(() => {
    const timer = setTimeout(() => {
      const queueToSave = fileQueue.map(f => {
        const { file, ...rest } = f;
        return rest;
      });
      dbStore.set('linhhoat_queue', queueToSave);
    }, 1000);
    return () => clearTimeout(timer);
  }, [fileQueue]);

  useEffect(() => {
    const timer = setTimeout(() => {
      dbStore.set('linhhoat_history', history);
    }, 1000);
    return () => clearTimeout(timer);
  }, [history]);

  useEffect(() => {
    const timer = setTimeout(() => {
      dbStore.set('linhhoat_logs', logs);
    }, 800);
    return () => clearTimeout(timer);
  }, [logs]);

  // Timer effect
  useEffect(() => {
    if (isTranslating) {
      actualStartTimeRef.current = Date.now();
      setElapsedTime(0);
      timerRef.current = setInterval(() => {
        if (actualStartTimeRef.current) {
          const delta = Math.floor((Date.now() - actualStartTimeRef.current) / 1000);
          setElapsedTime(delta);
        }
      }, 1000);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
      actualStartTimeRef.current = null;
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [isTranslating]);

  const saveKeys = (keys: string[]) => {
    try {
      localStorage.setItem('linhhoat_keys', JSON.stringify(keys));
    } catch (e) {}
    let geminiKey = "";
    try {
      geminiKey = process.env.GEMINI_API_KEY || "";
    } catch (e) {}
    
    setApiKeys([geminiKey, ...keys].filter(k => !!k));
    setAdditionalKeys(keys);
  };

  const addToHistory = (name: string, content: string, size: number, type: 'text' | 'file') => {
    const newItem: HistoryItem = {
      id: Math.random().toString(36).substring(7),
      name: name || (type === 'text' ? 'Văn bản đã dịch' : 'Tệp không tên'),
      content: content,
      size,
      type,
      timestamp: Date.now(),
    };
    // Reduced standard limit from 50 to 30 for safety with potentially large files
    setHistory(prev => [newItem, ...prev].slice(0, 30));
  };

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const handleTranslateText = async () => {
    if (!inputText.trim()) return;
    setIsTranslating(true);
    setGlobalProgress(0);
    setError(null);
    setElapsedTime(0);
    actualStartTimeRef.current = Date.now();
    setLogs([]);
    addLog("Bắt đầu dịch văn bản...");

    try {
      console.log("Translation started for text of length:", inputText.length);
      addLog(`Bắt đầu dịch văn bản (${inputText.length} ký tự)...`);
      const result = await translateLargeText(
        inputText, 
        (p) => setGlobalProgress(p),
        (msg) => {
          console.log("Gemini log:", msg);
          addLog(msg);
        },
        (partial) => {
          setTranslatedText(partial);
        }
      );
      setTranslatedText(result);
      console.log("Translation completed. Result length:", result.length);
      addToHistory(inputText.substring(0, 30) + '...', result, inputText.length, 'text');
      addLog("Hoàn thành dịch văn bản!");
    } catch (err: any) {
      console.error("Translation error:", err);
      setError('Đã xảy ra lỗi khi dịch văn bản. Vui lòng thử lại sau.');
      addLog(`LỖI: ${err.message}`, "error");
    } finally {
      setIsTranslating(false);
    }
  };

  const handleFileChange = async (e: ChangeEvent<HTMLInputElement>) => {
    const filesList = e.target.files;
    if (filesList && filesList.length > 0) {
      const filesArr = Array.from(filesList) as File[];
      const allowedFiles = filesArr.filter(file => file.name.endsWith('.srt') || file.name.endsWith('.vtt'));
      
      const newFiles: QueuedFile[] = [];
      for (const file of allowedFiles) {
        try {
          const content = await file.text();
          newFiles.push({
            id: Math.random().toString(36).substring(7),
            file: file,
            name: file.name,
            size: file.size,
            status: 'pending',
            progress: 0,
            rawContent: content
          });
        } catch (err: any) {
          addLog(`Lỗi đọc tệp ${file.name}: ${err.message}`, 'error');
        }
      }

      if (newFiles.length === 0 && allowedFiles.length === 0) {
        setError('Vui lòng chọn tệp .srt hoặc .vtt');
      } else {
        setFileQueue(prev => [...prev, ...newFiles]);
        setError(null);
      }
    }
  };

  const removeFromFileQueue = (id: string) => {
    setFileQueue(prev => prev.filter(f => f.id !== id));
  };

  const clearQueue = () => {
    setFileQueue([]);
  };

  const pauseTranslation = (id: string) => {
    pausedFileIdsRef.current.add(id);
    setFileQueue(prev => prev.map(f => f.id === id ? { ...f, status: 'paused' } : f));
    addLog(`Đã gửi yêu cầu tạm dừng dịch tệp phụ đề...`);
  };

  const translateSingleFile = async (id: string) => {
    if (isTranslating) return;
    const targetFile = fileQueue.find(f => f.id === id);
    if (!targetFile) return;

    // Clear pause state for this file ID
    pausedFileIdsRef.current.delete(id);

    setIsTranslating(true);
    setError(null);
    setElapsedTime(0);
    actualStartTimeRef.current = Date.now();

    const content = targetFile.rawContent || (targetFile.file ? await targetFile.file.text() : null);
    if (!content) {
      addLog(`Không thể thực thi dịch: Không tìm thấy nội dung tệp ${targetFile.name}.`, 'error');
      setIsTranslating(false);
      return;
    }

    const alreadyTranslatedBlocks = targetFile.translatedBlocks || [];
    const doneLines = alreadyTranslatedBlocks.filter(b => b && b.length > 0).length;
    if (doneLines > 0) {
      addLog(`[KT TIẾN TRÌNH] Đang khôi phục và dịch tiếp tệp ${targetFile.name} (Đã dịch trước đó ${doneLines} câu phụ đề)...`);
    } else {
      addLog(`Bắt đầu xử lý dịch tệp: ${targetFile.name}`);
    }
    
    setFileQueue(prev => prev.map(f => f.id === targetFile.id ? { ...f, status: 'translating', error: undefined } : f));

    try {
      const translated = await translateSubtitleFileContent(
        content,
        (progress) => {
          setFileQueue(prev => prev.map(f => f.id === targetFile.id ? { ...f, progress } : f));
          setGlobalProgress(progress);
        },
        (msg) => addLog(msg),
        (partial) => {
          setFileQueue(prev => prev.map(f => f.id === targetFile.id ? { ...f, translatedContent: partial } : f));
        },
        alreadyTranslatedBlocks,
        (updatedBlocks, startIndex) => {
          setFileQueue(prev => prev.map(f => {
            if (f.id === targetFile.id) {
              return { 
                ...f, 
                translatedBlocks: updatedBlocks
              };
            }
            return f;
          }));
        },
        () => pausedFileIdsRef.current.has(targetFile.id)
      );

      setFileQueue(prev => prev.map(f => f.id === targetFile.id ? { 
        ...f, 
        status: 'completed', 
        progress: 100, 
        translatedContent: translated 
      } : f));

      addToHistory(targetFile.name, translated, targetFile.size, 'file');
      addLog(`Hoàn thành tệp ${targetFile.name}.`);
    } catch (err: any) {
      if (err.message === "PAUSED") {
        setFileQueue(prev => prev.map(f => f.id === targetFile.id ? { 
          ...f, 
          status: 'paused'
        } : f));
        addLog(`Đã tạm dừng dịch tệp ${targetFile.name}.`);
      } else {
        setFileQueue(prev => prev.map(f => f.id === targetFile.id ? { 
          ...f, 
          status: 'error', 
          error: 'Lỗi dịch thuật' 
        } : f));
        addLog(`Lỗi xử lý tệp ${targetFile.name}: ${err.message}`, "error");
      }
    } finally {
      setIsTranslating(false);
    }
  };

  const handleTranslateQueue = async () => {
    if (fileQueue.length === 0 || isTranslating) return;
    
    setIsTranslating(true);
    setError(null);
    setElapsedTime(0);
    actualStartTimeRef.current = Date.now();

    const pendingFiles = fileQueue.filter(f => f.status !== 'completed');
    
    for (const queuedFile of pendingFiles) {
      // Clear pause state for this file ID if starting translation
      pausedFileIdsRef.current.delete(queuedFile.id);

      const content = queuedFile.rawContent || (queuedFile.file ? await queuedFile.file.text() : null);
      if (!content) {
        addLog(`Bỏ qua tệp ${queuedFile.name} do không tìm thấy nội dung.`, 'error');
        continue;
      }
      
      const alreadyTranslatedBlocks = queuedFile.translatedBlocks || [];
      const doneLines = alreadyTranslatedBlocks.filter(b => b && b.length > 0).length;
      if (doneLines > 0) {
        addLog(`[HÀNG CHỜ] Đang nối tiếp dịch tệp: ${queuedFile.name} (Khôi phục đã dịch trước đó ${doneLines} câu phụ đề)`);
      } else {
        addLog(`[HÀNG CHỜ] Đang dịch tệp: ${queuedFile.name}`);
      }

      setFileQueue(prev => prev.map(f => f.id === queuedFile.id ? { ...f, status: 'translating', progress: doneLines > 0 ? (doneLines / (alreadyTranslatedBlocks.length || 1)) * 100 : 10 } : f));
      
      try {
        const translated = await translateSubtitleFileContent(
          content, 
          (progress) => {
            setFileQueue(prev => prev.map(f => f.id === queuedFile.id ? { ...f, progress } : f));
            setGlobalProgress(progress);
          }, 
          (msg) => addLog(msg), 
          (partial) => {
            setFileQueue(prev => prev.map(f => f.id === queuedFile.id ? { ...f, translatedContent: partial } : f));
          },
          alreadyTranslatedBlocks,
          (updatedBlocks, startIndex) => {
            setFileQueue(prev => prev.map(f => {
              if (f.id === queuedFile.id) {
                return { 
                  ...f, 
                  translatedBlocks: updatedBlocks
                };
              }
              return f;
            }));
          },
          () => pausedFileIdsRef.current.has(queuedFile.id)
        );
        
        setFileQueue(prev => prev.map(f => f.id === queuedFile.id ? { 
          ...f, 
          status: 'completed', 
          progress: 100, 
          translatedContent: translated 
        } : f));

        addToHistory(queuedFile.name, translated, queuedFile.size, 'file');
        addLog(`Hoàn thành tệp ${queuedFile.name}.`);
      } catch (err: any) {
        if (err.message === "PAUSED") {
          setFileQueue(prev => prev.map(f => f.id === queuedFile.id ? { 
            ...f, 
            status: 'paused'
          } : f));
          addLog(`Đã tạm dừng dịch tệp ${queuedFile.name}.`);
          break; // Break the queue processing loop
        } else {
          setFileQueue(prev => prev.map(f => f.id === queuedFile.id ? { 
            ...f, 
            status: 'error', 
            error: 'Lỗi dịch thuật' 
          } : f));
          addLog(`Lỗi xử lý tệp ${queuedFile.name}: ${err.message}`, "error");
        }
      }
    }
    
    setIsTranslating(false);
  };

  const downloadFile = (name: string, content: string) => {
    const blob = new Blob([content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name.startsWith('[VI]_') ? name : `[VI]_${name}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const downloadAllCompleted = () => {
    fileQueue.forEach(f => {
      if (f.status === 'completed' && f.translatedContent) {
        downloadFile(f.name, f.translatedContent);
      }
    });
  };

  const removeFromHistory = (id: string) => {
    setHistory(prev => prev.filter(h => h.id !== id));
  };

  const copyToClipboard = async (text: string) => {
    if (!text) return;
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(text);
        addLog("Đã sao chép vào bộ nhớ tạm.");
      } else {
        throw new Error("Trình duyệt không hỗ trợ Clipboard API");
      }
    } catch (err: any) {
      addLog("Lỗi khi sao chép: " + err.message, "error");
    }
  };

  return (
    <div className="min-h-screen flex flex-col font-sans">
      {/* Navbar */}
      <nav className="sticky top-0 z-50 glass border-b border-gray-200">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4 flex justify-between items-center">
          <div className="flex items-center gap-2">
            <div className="bg-primary-600 p-2 rounded-xl">
              <Languages className="w-6 h-6 text-white" />
            </div>
            <span className="font-display font-bold text-xl tracking-tight text-primary-900">
              LinhHoat AI
            </span>
          </div>
          
          <div className="hidden md:flex items-center gap-8 text-sm font-medium text-gray-600">
            <button onClick={() => setActiveTab('text')} className={`hover:text-primary-600 transition-colors ${activeTab === 'text' ? 'text-primary-600' : ''}`}>Dịch Văn Bản</button>
            <button onClick={() => setActiveTab('file')} className={`hover:text-primary-600 transition-colors ${activeTab === 'file' ? 'text-primary-600' : ''}`}>Hàng Chờ Phụ Đề</button>
            <button onClick={() => setActiveTab('history')} className={`hover:text-primary-600 transition-colors flex items-center gap-2 ${activeTab === 'history' ? 'text-primary-600' : ''}`}>
              <Clock className="w-4 h-4" /> Lịch Sử ({history.length})
            </button>
          </div>

          <div className="flex items-center gap-4">
            <button className="bg-gray-900 text-white px-6 py-2 rounded-full font-medium hover:bg-gray-800 transition-all text-sm shadow-lg shadow-gray-200">
              Bản dịch Miễn Phí
            </button>
          </div>
        </div>
      </nav>

      <main className="flex-1">
        {/* Section Header */}
        <section className="relative py-12 px-4 overflow-hidden">
          <div className="absolute top-0 right-0 -z-10 translate-x-1/2 -translate-y-1/4">
            <div className="w-[600px] h-[600px] bg-primary-100/50 rounded-full blur-3xl" />
          </div>
          
          <div className="max-w-4xl mx-auto text-center space-y-4">
            <motion.h1 
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              className="text-4xl md:text-5xl font-display font-bold text-gray-900 leading-tight"
            >
              {activeTab === 'text' && "Dịch Văn Bản"}
              {activeTab === 'file' && "Dịch Phụ Đề Hàng Loạt"}
              {activeTab === 'history' && "Lịch Sử Dịch Thuật"}
            </motion.h1>
            <p className="text-gray-500">Mọi bản dịch của bạn đều được tự động lưu lại.</p>
          </div>
        </section>

        {/* Work Area */}
        <section className="max-w-6xl mx-auto px-4 pb-20">
          <div className="bg-white rounded-3xl shadow-2xl shadow-primary-900/5 border border-gray-100 overflow-hidden ring-1 ring-gray-200/50">
            {/* Tabs */}
            <div className="flex border-b border-gray-100 px-6 pt-6">
              {[
                { id: 'text', icon: FileText, label: 'Văn Bản' },
                { id: 'file', icon: Upload, label: 'Hàng Chờ' },
                { id: 'history', icon: History, label: 'Lịch Sử' },
                { id: 'settings', icon: Settings, label: 'Cấu Hình' }
              ].map((tab) => (
                <button 
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id as Tab)}
                  className={`flex items-center gap-2 pb-4 px-4 text-sm font-semibold transition-all relative ${
                    activeTab === tab.id ? 'text-primary-600' : 'text-gray-400 hover:text-gray-600'
                  }`}
                >
                  <tab.icon className="w-4 h-4" />
                  {tab.label}
                  {activeTab === tab.id && (
                    <motion.div layoutId="tab-active" className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary-600" />
                  )}
                </button>
              ))}
            </div>

            <div className="p-6 md:p-10">
              {activeTab === 'text' && (
                <div className="grid md:grid-cols-2 gap-8">
                  <div className="space-y-4">
                    <div className="flex items-center justify-between font-bold text-[10px] text-gray-400 uppercase tracking-widest">
                      <span>TIẾNG TRUNG</span>
                      <ArrowRightLeft className="w-4 h-4" />
                    </div>
                    <textarea 
                      value={inputText}
                      onChange={(e) => setInputText(e.target.value)}
                      placeholder="Nhập nội dung cần dịch..."
                      className="w-full h-96 p-6 bg-gray-50 border-none rounded-2xl focus:ring-2 focus:ring-primary-100 transition-all resize-none"
                    />
                    <button 
                      onClick={handleTranslateText}
                      disabled={isTranslating || !inputText}
                      className="w-full bg-primary-600 text-white py-4 rounded-xl font-bold flex items-center justify-center gap-3 hover:bg-primary-700 disabled:opacity-50 shadow-lg relative overflow-hidden"
                    >
                      {isTranslating ? (
                        <>
                          <Loader2 className="animate-spin w-5 h-5" />
                          <span>Đang dịch... ({formatTime(elapsedTime)})</span>
                          <div 
                            className="absolute bottom-0 left-0 h-1 bg-white/30 transition-all duration-300" 
                            style={{ width: `${globalProgress}%` }}
                          />
                        </>
                      ) : (
                        <>
                          <Play className="w-5 h-5" />
                          <span>Dịch Ngay</span>
                        </>
                      )}
                    </button>
                  </div>
                  <div className="space-y-4">
                    <div className="flex items-center justify-between font-bold text-[10px] text-gray-400 uppercase tracking-widest">
                      <span>TIẾNG VIỆT</span>
                      <div className="flex gap-4">
                        {translatedText && (
                          <>
                            <button onClick={() => {
                              const blob = new Blob([translatedText], { type: 'text/plain' });
                              const url = URL.createObjectURL(blob);
                              const a = document.createElement('a');
                              a.href = url;
                              a.download = `translated_${Date.now()}.txt`;
                              a.click();
                              URL.revokeObjectURL(url);
                            }} className="text-primary-600 hover:underline">TẢI VỀ (.TXT)</button>
                            <button onClick={() => copyToClipboard(translatedText)} className="text-primary-600 hover:underline">SAO CHÉP</button>
                          </>
                        )}
                      </div>
                    </div>
                    <textarea 
                      readOnly
                      value={translatedText}
                      placeholder="Kết quả sẽ hiển thị ở đây..."
                      className={`w-full h-96 p-6 rounded-2xl transition-all resize-none outline-none font-mono text-sm ${
                        isTranslating ? 'bg-gray-50/50 text-gray-300 italic' : 'bg-primary-50/20 text-gray-800'
                      }`}
                    />
                  </div>
                </div>
              )}

              {activeTab === 'settings' && (
                <div className="space-y-8 max-w-2xl mx-auto py-10">
                  <div className="bg-primary-50 p-6 rounded-3xl border border-primary-100">
                    <div className="flex items-center gap-4 mb-4">
                      <Settings className="w-6 h-6 text-primary-600" />
                      <h3 className="font-bold text-gray-800">Cấu hình API Gemini</h3>
                    </div>
                    <p className="text-sm text-gray-500 mb-6 leading-relaxed">
                      LinhHoat AI sử dụng khóa API mặc định. Nếu bạn gặp lỗi hết hạn mức hoặc muốn dịch nhanh hơn, bạn có thể thêm các khóa API dự phòng của riêng mình tại đây. Chúng sẽ được lưu an toàn trong trình duyệt của bạn.
                    </p>
                    
                    <div className="space-y-4">
                      <div className="space-y-2">
                        <label className="text-[10px] font-bold text-gray-400 uppercase">Thêm API Keys (mỗi dòng một key)</label>
                        <textarea 
                          defaultValue={additionalKeys.join('\n')}
                          onBlur={(e) => {
                            const keys = e.target.value.split('\n').map(k => k.trim()).filter(k => k.length > 0);
                            saveKeys(keys);
                          }}
                          className="w-full h-32 p-4 bg-white border border-gray-200 rounded-xl focus:ring-2 focus:ring-primary-100 outline-none font-mono text-sm"
                          placeholder="AIzaSy..."
                        />
                      </div>
                      <div className="flex items-center gap-2 text-[10px] text-primary-600 font-bold bg-white px-3 py-2 rounded-lg border border-primary-50">
                        <CheckCircle2 className="w-3 h-3" />
                        Đang sử dụng pool: {1 + additionalKeys.length} keys
                      </div>
                    </div>
                  </div>
                  
                  <div className="bg-orange-50 p-6 rounded-3xl border border-orange-100 flex gap-4">
                    <AlertCircle className="w-5 h-5 text-orange-500 shrink-0" />
                    <div>
                      <p className="text-sm font-bold text-orange-800 mb-1">Mẹo nâng cao</p>
                      <p className="text-xs text-orange-700 leading-relaxed">
                        Để đạt tốc độ dịch cao nhất cho file SRT lớn, chúng tôi khuyên bạn nên sử dụng model <b>Gemini 1.5 Flash</b>. Bạn có thể tạo API key miễn phí tại Google AI Studio.
                      </p>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'file' && (
                <div className="space-y-8">
                  <div className="grid lg:grid-cols-3 gap-8">
                    <div className="lg:col-span-1 space-y-4">
                      <div 
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={(e) => {
                          e.preventDefault();
                          const files = e.dataTransfer.files;
                          if (files.length > 0) handleFileChange({ target: { files } } as any);
                        }}
                        onClick={() => fileInputRef.current?.click()}
                        className="w-full h-64 flex flex-col items-center justify-center border-2 border-dashed border-gray-200 rounded-3xl bg-gray-50 hover:bg-white hover:border-primary-400 transition-all cursor-pointer group"
                      >
                        <input type="file" ref={fileInputRef} onChange={handleFileChange} accept=".srt,.vtt" multiple className="hidden" />
                        <Upload className="w-8 h-8 text-primary-400 group-hover:scale-110 transition-transform mb-4" />
                        <p className="font-bold text-gray-700 text-sm">Chọn tệp SRT/VTT</p>
                      </div>

                      {fileQueue.length > 0 && (
                        <div className="space-y-3">
                          <button onClick={handleTranslateQueue} disabled={isTranslating} className="w-full bg-primary-600 text-white py-4 rounded-xl font-bold flex items-center justify-center gap-2 hover:bg-primary-700 transition-all shadow-md relative overflow-hidden">
                            {isTranslating ? (
                              <>
                                <Loader2 className="animate-spin w-4 h-4" /> 
                                <span>Dịch Hàng Chờ ({formatTime(elapsedTime)})</span>
                              </>
                            ) : (
                              <>
                                <Play className="w-4 h-4" /> 
                                <span>Dịch Hàng Chờ</span>
                              </>
                            )}
                          </button>
                          <div className="flex gap-2">
                             <button onClick={downloadAllCompleted} disabled={!fileQueue.some(f => f.status === 'completed')} className="flex-1 bg-white border border-gray-200 py-3 rounded-xl text-xs font-bold flex items-center justify-center gap-2 hover:bg-gray-50">
                              <FileDown className="w-4 h-4" /> Tải về hết
                            </button>
                            <button onClick={clearQueue} className="flex-1 bg-white border border-gray-200 py-3 rounded-xl text-xs font-bold flex items-center justify-center gap-2 hover:text-red-600 hover:border-red-200">
                              <Trash2 className="w-4 h-4" /> Làm mới
                            </button>
                          </div>
                        </div>
                      )}
                    </div>

                    <div className="lg:col-span-2 bg-gray-50/50 rounded-3xl p-4 border border-gray-100 min-h-[400px]">
                      <div className="flex flex-col h-full bg-white rounded-2xl shadow-sm border border-gray-50 overflow-hidden">
                        <div className="px-6 py-4 border-b border-gray-50 flex justify-between items-center text-xs font-bold text-gray-400">
                          <span>DANH SÁCH DỊCH</span>
                          <span>{fileQueue.length} Tệp</span>
                        </div>
                        <div className="flex-1 overflow-y-auto divide-y divide-gray-50">
                          {fileQueue.length === 0 ? (
                            <div className="h-full flex flex-col items-center justify-center text-gray-300 p-10">
                              <History className="w-10 h-10 mb-2 opacity-10" />
                              <p className="text-sm">Chưa có tệp nào</p>
                            </div>
                          ) : (
                            fileQueue.map(item => {
                              const totalCount = item.translatedBlocks?.length || item.chunksCount || 0;
                              const translatedCount = item.translatedBlocks 
                                ? item.translatedBlocks.filter(b => b && b.length > 0).length 
                                : (item.translatedChunks?.length || 0);
                              const hasProgress = translatedCount > 0;
                              
                              return (
                                <div key={item.id} className="p-4 flex flex-col md:flex-row md:items-center justify-between gap-4 hover:bg-gray-50/80 transition-all border-b border-gray-100 last:border-0">
                                  <div className="flex items-center gap-4 flex-1 min-w-0">
                                    <div className={`p-3 rounded-xl shrink-0 ${
                                      item.status === 'completed' ? 'bg-green-100 text-green-600' :
                                      item.status === 'translating' ? 'bg-primary-100 text-primary-600 animate-pulse' :
                                      item.status === 'paused' ? 'bg-amber-100 text-amber-600' :
                                      item.status === 'interrupted' ? 'bg-amber-100 text-amber-600' :
                                      item.status === 'error' ? 'bg-red-100 text-red-600' : 'bg-gray-100 text-gray-400'
                                    }`}>
                                      {item.status === 'completed' ? (
                                        <CheckCircle2 className="w-5 h-5 animate-bounce" />
                                      ) : item.status === 'translating' ? (
                                        <Loader2 className="w-5 h-5 animate-spin" />
                                      ) : item.status === 'paused' ? (
                                        <Pause className="w-5 h-5 animate-pulse" />
                                      ) : item.status === 'interrupted' ? (
                                        <AlertTriangle className="w-5 h-5" />
                                      ) : item.status === 'error' ? (
                                        <XCircle className="w-5 h-5" />
                                      ) : (
                                        <FileText className="w-5 h-5" />
                                      )}
                                    </div>
                                    <div className="flex-1 min-w-0">
                                      <p className="text-sm font-bold text-gray-800 truncate">{item.name}</p>
                                      <div className="flex flex-wrap items-center gap-2 mt-1">
                                        <span className="text-[10px] text-gray-400 font-mono uppercase tracking-tight font-bold font-bold">{(item.size / 1024).toFixed(1)} KB</span>
                                        <span className="text-gray-300">•</span>
                                        
                                        {/* Status badges with progress bar representation */}
                                        {item.status === 'completed' && (
                                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-green-50 text-green-600 border border-green-100">
                                            Đã dịch xong
                                          </span>
                                        )}
                                        {item.status === 'translating' && (
                                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-primary-50 text-primary-600 border border-primary-200 animate-pulse">
                                            Đang dịch: {translatedCount}/{totalCount || '?'} câu ({item.progress.toFixed(0)}%)
                                          </span>
                                        )}
                                        {item.status === 'paused' && (
                                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-50 text-amber-600 border border-amber-200 animate-pulse">
                                            Đã tạm dừng: {translatedCount}/{totalCount || '?'} câu ({item.progress.toFixed(0)}%)
                                          </span>
                                        )}
                                        {item.status === 'interrupted' && (
                                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-50 text-amber-600 border border-amber-200" title="Trình duyệt bị tắt đột ngột. Bạn có thể dịch tiếp từ cụm này!">
                                            Đã gián đoạn: {hasProgress ? `Đã xong ${translatedCount}/${totalCount} câu` : 'Chưa dịch'}
                                          </span>
                                        )}
                                        {item.status === 'error' && (
                                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-red-50 text-red-600 border border-red-200">
                                            Gặp sự cố {hasProgress ? `(Đã dịch ${translatedCount}/${totalCount} câu)` : 'Chưa dịch'}
                                          </span>
                                        )}
                                        {item.status === 'pending' && (
                                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold bg-gray-50 text-gray-500 border border-gray-200">
                                            Đang đợi dịch
                                          </span>
                                        )}
                                      </div>

                                      {/* Mini progress bar */}
                                      {item.status !== 'pending' && (
                                        <div className="w-full bg-gray-100 h-1.5 rounded-full mt-2 overflow-hidden">
                                          <div 
                                            className={`h-full transition-all duration-300 ${
                                              item.status === 'completed' ? 'bg-green-500' :
                                              item.status === 'translating' ? 'bg-primary-500' :
                                              item.status === 'paused' ? 'bg-amber-500' :
                                              item.status === 'interrupted' ? 'bg-amber-400' : 'bg-red-400'
                                            }`}
                                            style={{ width: `${item.progress}%` }}
                                          />
                                        </div>
                                      )}
                                    </div>
                                  </div>

                                  <div className="flex items-center gap-2 justify-end shrink-0">
                                    {/* Action button to Pause translating file */}
                                    {item.status === 'translating' && (
                                      <button 
                                        onClick={() => pauseTranslation(item.id)} 
                                        className="px-3 py-1.5 rounded-xl text-xs font-bold flex items-center gap-1.5 transition-all bg-amber-500 text-white hover:bg-amber-600 shadow-sm"
                                        title="Tạm dừng dịch tệp này"
                                      >
                                        <Pause className="w-3.5 h-3.5" />
                                        <span>Tạm dừng</span>
                                      </button>
                                    )}

                                    {/* Action button to Translate or Resume Single File */}
                                    {item.status !== 'completed' && item.status !== 'translating' && (
                                      <button 
                                        onClick={() => translateSingleFile(item.id)} 
                                        disabled={isTranslating}
                                        className={`px-3 py-1.5 rounded-xl text-xs font-bold flex items-center gap-1.5 transition-all shadow-sm ${
                                          hasProgress 
                                            ? 'bg-amber-500 text-white hover:bg-amber-600' 
                                            : 'bg-primary-650 text-white bg-primary-600 hover:bg-primary-700'
                                        } disabled:opacity-50`}
                                      >
                                        <Play className="w-3.5 h-3.5" />
                                        <span>{hasProgress ? `Dịch tiếp (${translatedCount}/${totalCount})` : 'Dịch tệp này'}</span>
                                      </button>
                                    )}

                                    {item.status === 'completed' && item.translatedContent && (
                                      <button 
                                        onClick={() => downloadFile(item.name, item.translatedContent!)} 
                                        className="p-2.5 bg-green-50 hover:bg-green-100 text-green-600 rounded-xl transition-colors border border-green-200/50"
                                        title="Tải phụ đề dịch"
                                      >
                                        <Download className="w-4 h-4" />
                                      </button>
                                    )}
                                    <button 
                                      onClick={() => removeFromFileQueue(item.id)} 
                                      className="p-2.5 bg-gray-50 hover:bg-red-50 text-gray-400 hover:text-red-500 rounded-xl transition-all border border-gray-100 hover:border-red-100"
                                      title="Xóa khỏi hàng chờ"
                                    >
                                      <Trash2 className="w-4 h-4" />
                                    </button>
                                  </div>
                                </div>
                              );
                            })
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'history' && (
                <div className="space-y-6 min-h-[500px]">
                  <div className="flex items-center justify-between">
                    <h3 className="font-bold text-gray-800">Lịch sử gần đây</h3>
                    <button onClick={() => { if(confirm('Xóa sạch lịch sử?')) setHistory([]); }} className="text-xs font-bold text-red-500 hover:underline">Xóa tất cả</button>
                  </div>
                  
                  {history.length === 0 ? (
                    <div className="flex flex-col items-center justify-center py-20 text-gray-300">
                      <Clock className="w-16 h-16 opacity-10 mb-4" />
                      <p className="font-medium">Bạn chưa thực hiện bản dịch nào</p>
                    </div>
                  ) : (
                    <div className="grid gap-4 md:grid-cols-2">
                      {history.map(item => (
                        <div key={item.id} className="bg-gray-50/50 rounded-2xl p-5 border border-gray-100 flex flex-col gap-4 group hover:bg-white hover:shadow-xl transition-all">
                          <div className="flex justify-between items-start">
                            <div className="flex items-center gap-3">
                              <div className={`p-2.5 rounded-xl ${item.type === 'file' ? 'bg-primary-100 text-primary-600' : 'bg-indigo-100 text-indigo-600'}`}>
                                {item.type === 'file' ? <FileText className="w-5 h-5" /> : <Languages className="w-5 h-5" />}
                              </div>
                              <div>
                                <p className="text-sm font-bold text-gray-800 truncate max-w-[200px]">{item.name}</p>
                                <p className="text-[10px] text-gray-400">{new Date(item.timestamp).toLocaleString('vi-VN')}</p>
                              </div>
                            </div>
                            <button onClick={() => removeFromHistory(item.id)} className="p-2 hover:bg-red-50 text-gray-300 hover:text-red-500 rounded-lg opacity-0 group-hover:opacity-100 transition-all">
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </div>
                          <div className="bg-white p-3 rounded-xl border border-gray-50 text-xs text-gray-500 h-24 overflow-hidden overflow-ellipsis line-clamp-4 relative">
                             {item.content.length > 1000 ? item.content.substring(0, 1000) + '...' : item.content}
                             <div className="absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-white to-transparent" />
                          </div>
                          <div className="flex gap-2">
                            <button onClick={() => downloadFile(item.name, item.content)} className="flex-1 bg-white border border-gray-100 py-2.5 rounded-xl text-xs font-bold flex items-center justify-center gap-2 hover:bg-primary-50 hover:text-primary-600 transition-colors">
                              <Download className="w-3.5 h-3.5" /> Tải về
                            </button>
                            <button onClick={() => { setTranslatedText(item.content); setActiveTab('text'); }} className="flex-1 bg-white border border-gray-200 py-2.5 rounded-xl text-xs font-bold flex items-center justify-center gap-2 hover:bg-gray-50 transition-colors">
                              <Eye className="w-3.5 h-3.5" /> Xem lại
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Trạng thái hoạt động thời gian thực (Activity Heartbeat Tracker) */}
              <div className="mt-8 p-4 bg-gray-50/80 border border-gray-100 rounded-2xl flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <div className="relative flex h-3 w-3">
                    <span className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${isTranslating ? 'bg-green-400' : 'bg-primary-400'}`}></span>
                    <span className={`relative inline-flex rounded-full h-3 w-3 ${isTranslating ? 'bg-green-500' : 'bg-primary-500'}`}></span>
                  </div>
                  <div>
                    <p className="text-xs font-bold text-gray-700 flex items-center gap-1.5">
                      Trạng thái hoạt động: 
                      <span className={isTranslating ? 'text-green-600' : 'text-primary-600'}>
                        {isTranslating ? 'ĐANG TIÊN HÀNH CHUYỂN NGỮ' : 'ĐANG CHỜ TÁC VỤ'}
                      </span>
                    </p>
                    <p className="text-[10px] text-gray-400">
                      {isTranslating 
                        ? `Đang thực hiện cuộc gọi API thông qua pool khóa Gemini an toàn (Thời gian thực thi: ${formatTime(elapsedTime)})` 
                        : 'Hệ thống rảnh rỗi. Dữ liệu tiến trình của các tệp dịch dở dang đều được tự động lưu an toàn vào cơ sở dữ liệu.'}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-4 text-[10px] font-bold text-gray-400 bg-white px-3 py-2 rounded-xl border border-gray-100/55 shadow-sm max-w-fit">
                  <div className="flex items-center gap-1 text-green-600">
                    <Wifi className="w-3.5 h-3.5" />
                    <span>Mạng Hoạt Động</span>
                  </div>
                  <span className="text-gray-200">|</span>
                  <div className="flex items-center gap-1 text-primary-600">
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    <span>Đã lưu IndexedDB</span>
                  </div>
                </div>
              </div>

              {logs.length > 0 && (
                <motion.div 
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  className="mt-8 border-t border-gray-100 pt-6"
                >
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 bg-primary-500 rounded-full animate-pulse" />
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-widest">TIẾN TRÌNH HỆ THỐNG (LOGS)</h4>
                    </div>
                    <button onClick={() => setLogs([])} className="text-[10px] font-bold text-red-400 hover:text-red-600">XÓA LOG</button>
                  </div>
                  <div 
                    ref={scrollRef}
                    className="bg-gray-900 rounded-xl p-4 h-40 overflow-y-auto font-mono text-[11px] leading-relaxed select-text"
                  >
                    {logs.map((log, i) => (
                      <div key={i} className="flex gap-3 mb-1.5 last:mb-0">
                        <span className="text-gray-500 shrink-0">[{log.time}]</span>
                        <span className={log.type === 'error' ? 'text-red-400' : 'text-primary-300'}>
                          {log.msg}
                        </span>
                      </div>
                    ))}
                  </div>
                </motion.div>
              )}

                  {error && (
                    <div className="flex items-center gap-2 p-4 bg-red-50 text-red-600 rounded-xl text-sm font-medium border border-red-100 animate-in fade-in slide-in-from-top-2">
                      <AlertCircle className="w-4 h-4" />
                      {error}
                    </div>
                  )}
                </div>
              </div>
            </section>
          </main>

      {/* Footer */}
      <footer className="bg-gray-50 border-t border-gray-200 py-12 px-4">
        <div className="max-w-6xl mx-auto flex flex-col md:flex-row justify-between items-center gap-8">
          <div className="flex items-center gap-2">
            <div className="bg-gray-300 p-1.5 rounded-lg">
              <Languages className="w-4 h-4 text-white" />
            </div>
            <span className="font-display font-bold text-gray-900 tracking-tight">
              LinhHoat AI
            </span>
          </div>
          
          <div className="text-gray-400 text-sm">
            © 2026 LinhHoat AI. Phát triển bởi Google AI Studio.
          </div>

          <div className="flex gap-6 items-center">
            <a href="#" className="text-gray-400 hover:text-gray-600 transition-colors">
              <Github className="w-5 h-5" />
            </a>
            <a href="#" className="text-gray-400 hover:text-gray-600 transition-colors">
              <History className="w-5 h-5" />
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}
