import { Tooltip, Typography } from 'antd';
import type { ReactNode } from 'react';

/** 业务界面显示名称/编号；真实 ID 仅在悬停或键盘聚焦时展示，可复制排查。 */
export default function EntityLabel({
  id,
  children,
}: {
  id: string;
  children: ReactNode;
}) {
  return (
    <Tooltip
      trigger={['hover', 'focus']}
      title={
        <Typography.Text
          code
          copyable={{ text: id }}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
          style={{ color: 'inherit' }}
        >
          {id}
        </Typography.Text>
      }
    >
      {/* biome-ignore lint/a11y/noNoninteractiveTabindex: 聚焦后可读取和复制 ID；组件也用于按钮内部，不能再嵌套按钮。 */}
      <span tabIndex={0} data-entity-id={id}>
        {children}
      </span>
    </Tooltip>
  );
}
