"use client";

import { useEffect, useRef, type HTMLAttributes, type ReactNode } from "react";

type Props = {
  children: ReactNode;
} & Omit<HTMLAttributes<HTMLDivElement>, "children">;

/**
 * For nested `overflow-y: auto` lists (e.g. party names in a cell). When the list is
 * at the top or bottom, forward wheel to `document.scrollingElement` so the main page
 * can scroll. Also forwards when the list is short and has no vertical overflow.
 */
export function ScrollAreaForwardWheelToPageAtEdge({
  children,
  className,
  ...rest
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const onWheel = (e: WheelEvent) => {
      if (e.shiftKey) return;
      const root = document.scrollingElement;
      if (!root) return;

      const { scrollTop, scrollHeight, clientHeight } = el;
      const atTop = scrollTop <= 0;
      const atBottom = scrollTop + clientHeight >= scrollHeight - 0.5;
      const canScrollY = scrollHeight > clientHeight + 1;

      if (!canScrollY) {
        e.preventDefault();
        root.scrollBy({ top: e.deltaY, left: e.deltaX, behavior: "auto" });
        return;
      }
      if (atTop && e.deltaY < 0) {
        e.preventDefault();
        root.scrollBy({ top: e.deltaY, left: 0, behavior: "auto" });
        return;
      }
      if (atBottom && e.deltaY > 0) {
        e.preventDefault();
        root.scrollBy({ top: e.deltaY, left: 0, behavior: "auto" });
        return;
      }
    };

    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  return (
    <div ref={ref} className={className} {...rest}>
      {children}
    </div>
  );
}
