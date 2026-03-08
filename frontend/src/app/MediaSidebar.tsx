import React, { useState, useEffect } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';

export interface DetectedMedia {
  url: string;
  type: 'image' | 'link';
  timestamp: number;
}

const MediaSidebar = ({ items }: { items: DetectedMedia[] }) => {
  const [isOpen, setIsOpen] = useState(true);
  const [lastCount, setLastCount] = useState(items.length);

  // Automatically expand when new items arrive
  useEffect(() => {
    if (items.length > lastCount) {
      setIsOpen(true);
    }
    setLastCount(items.length);
  }, [items.length, lastCount]);

  if (items.length === 0) return null;

  return (
    <div className="fixed right-4 top-4 w-48 md:w-64 z-20 pointer-events-none flex flex-col">
      <div className="bg-black/40 backdrop-blur-xl border border-white/10 rounded-2xl overflow-hidden flex flex-col max-h-[70vh] shadow-2xl pointer-events-auto animate-in fade-in slide-in-from-right-8 duration-700">
        <div 
          className="px-4 py-3 border-b border-white/10 bg-white/5 flex items-center justify-between cursor-pointer hover:bg-white/10 transition-colors"
          onClick={() => setIsOpen(!isOpen)}
        >
          <div className="flex items-center gap-2">
            <h3 className="text-[10px] font-bold text-gray-300 uppercase tracking-widest">Shared Content</h3>
            <span className="bg-blue-500/20 text-blue-400 text-[10px] px-2 py-0.5 rounded-full border border-blue-500/30">
              {items.length}
            </span>
          </div>
          {isOpen ? <ChevronUp size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
        </div>
        
        {isOpen && (
          <div className="overflow-y-auto p-3 flex flex-col gap-3 no-scrollbar animate-in slide-in-from-top-2 duration-300">
            {items.map((item) => (
              <div key={item.timestamp} className="group relative">
                {item.type === 'image' ? (
                  <div className="relative rounded-lg overflow-hidden border border-white/5 bg-black/20 hover:border-white/20 transition-all duration-300">
                    <a href={item.url} target="_blank" rel="noopener noreferrer" className="block">
                      <img src={item.url} alt="Shared" className="w-full h-auto object-cover hover:scale-105 transition-transform duration-500" />
                    </a>
                  </div>
                ) : (
                  <div className="p-2 rounded-lg bg-white/5 border border-white/5 hover:bg-white/10 hover:border-white/20 transition-all duration-300">
                    <a 
                      href={item.url} 
                      target="_blank" 
                      rel="noopener noreferrer"
                      className="text-[10px] md:text-xs text-blue-400 hover:text-blue-300 break-all underline flex items-center gap-2"
                    >
                      <span className="truncate">{item.url.replace(/^https?:\/\//, '')}</span>
                    </a>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

export default MediaSidebar;
