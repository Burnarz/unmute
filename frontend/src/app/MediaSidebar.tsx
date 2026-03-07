import React from 'react';

export interface DetectedMedia {
  url: string;
  type: 'image' | 'link';
  timestamp: number;
}

const MediaSidebar = ({ items }: { items: DetectedMedia[] }) => {
  if (items.length === 0) return null;

  return (
    <div className="fixed right-4 top-24 bottom-32 w-48 md:w-64 overflow-y-auto flex flex-col gap-4 z-20 pointer-events-auto no-scrollbar">
      <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider px-2">Media & Links</h3>
      {items.map((item) => (
        <div key={item.timestamp} className="bg-black/40 backdrop-blur-md border border-white/10 rounded-lg p-2 animate-in fade-in slide-in-from-right-4 duration-500 hover:bg-black/60 transition-colors">
          {item.type === 'image' ? (
            <a href={item.url} target="_blank" rel="noopener noreferrer" className="block">
              <img src={item.url} alt="Shared content" className="w-full h-auto rounded border border-white/5" />
            </a>
          ) : (
            <a 
              href={item.url} 
              target="_blank" 
              rel="noopener noreferrer"
              className="text-[10px] md:text-xs text-blue-400 hover:text-blue-300 break-all underline"
            >
              {item.url}
            </a>
          )}
        </div>
      ))}
    </div>
  );
};

export default MediaSidebar;
