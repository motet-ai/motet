/**
 * Motet - Chat Explorer - Chat Thread
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-01-01
 *
 * Description:
 *     Scrollable chat transcript container that displays message bubbles.
 *
 *     Features:
 *     - Auto-scroll to bottom on new messages
 *     - Uses Ant Design X Bubble.List for message rendering
 *     - Styled with theme tokens for consistent appearance
 *     - Smooth scroll behavior via CSS
 *
 * Dependencies:
 *     - @ant-design/x: Bubble component (Bubble.List)
 *     - React: useRef, useEffect for auto-scroll
 *
 * Usage:
 *     <ChatThread token={token} items={bubbleListItems} />
 *
 * Notes:
 *     - items come from useMotetChat's bubbleListItems
 *     - Auto-scroll triggers on every items change
 */
import React, { useRef, useEffect } from "react";
import { Bubble } from "@ant-design/x";

/**
 * Props for the ChatThread component.
 */
interface ChatThreadProps {
  /** Ant Design theme token for consistent styling */
  token: any;
  /** Array of Bubble.List items from useMotetChat */
  items: any[];
}

/**
 * Scrollable chat message display component.
 * Automatically scrolls to bottom when new messages arrive.
 */
export function ChatThread({ token, items }: ChatThreadProps) {
  // Ref to the scrollable container for programmatic scrolling
  const chatThreadRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when items change (new message or streaming update)
  useEffect(() => {
    if (chatThreadRef.current) {
      chatThreadRef.current.scrollTop = chatThreadRef.current.scrollHeight;
    }
  }, [items]);

  return (
    <div 
      className="chat-thread" 
      ref={chatThreadRef}
      style={{ 
        background: token.colorBgContainer,
        border: `1px solid ${token.colorBorder}`,
      }}
    >
      <Bubble.List
        items={items}
      />
    </div>
  );
}

