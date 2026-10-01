import { Button } from 'antd';
import type { ReactNode } from 'react';
import { useEffect, useId, useRef, useState } from 'react';
import './expandable.less';

/** 仅折叠展示，不截断原文；展开仍限高滚动，避免大段内容撑开工作区。 */
export default function ExpandableContent({
  text,
  label,
  children,
  previewHeight = 144,
}: {
  text: string;
  label: string;
  children: ReactNode;
  previewHeight?: number;
}) {
  const id = useId();
  const body = useRef<HTMLElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflow, setOverflow] = useState(
    text.length > 320 || text.split('\n').length > 6,
  );
  useEffect(() => {
    const el = body.current;
    if (!el) return;
    const measure = () => {
      setOverflow(
        el.clientWidth
          ? el.scrollHeight > previewHeight + 2
          : text.length > 320 || text.split('\n').length > 6,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, previewHeight]);
  return (
    <div
      className={`audit-expandable ${overflow && !expanded ? 'is-folded' : ''}`}
    >
      <section
        ref={body}
        id={id}
        className="audit-expandable-body"
        style={{
          maxHeight: expanded ? 360 : previewHeight,
          overflowY: expanded ? 'auto' : 'hidden',
        }}
        tabIndex={expanded ? 0 : undefined}
        aria-label={label}
      >
        {children}
      </section>
      {overflow && (
        <Button
          type="link"
          size="small"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => {
            if (body.current) body.current.scrollTop = 0;
            setExpanded((value) => !value);
          }}
        >
          {expanded ? '收起' : '展开'}
          {label}
        </Button>
      )}
    </div>
  );
}
