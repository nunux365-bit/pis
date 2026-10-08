"use client";

import { useEffect, useRef, type ReactNode } from "react";

const BASE_CLASS =
  "min-w-0 overflow-x-auto overscroll-x-contain [scrollbar-gutter:stable]";

type Props = {
  children: ReactNode;
  className?: string;
};

/**
 * Wide tables: keep horizontal pan predictable — `overscroll-contain` stops scroll
 * chaining into the page; non-passive wheel maps Shift+vertical → `scrollLeft`
 * (native `deltaX` is left to the browser).
 */
export function HorizontalTableScrollArea(props: Props) {
  const { children, className = "" } = props;
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const onWheel = (e: WheelEvent) => {
      if (el.scrollWidth <= el.clientWidth) return;
      // Native deltaX (trackpad / horizontal mouse wheel): browser handles; keep passive.
      if (Math.abs(e.deltaX) > 0) return;

      if (e.shiftKey && Math.abs(e.deltaY) > 0) {
        const max = el.scrollWidth - el.clientWidth;
        if (max <= 0) return;
        const before = el.scrollLeft;
        const next = Math.max(0, Math.min(max, el.scrollLeft + e.deltaY));
        if (next !== before) {
          el.scrollTo({ left: next, behavior: "auto" });
          e.preventDefault();
        }
      }
    };

    el.addEventListener("wheel", onWheel, { passive: false, capture: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  return (
    <div ref={ref} className={className ? `${BASE_CLASS} ${className}` : BASE_CLASS}>
      {children}
    </div>
  );
}
