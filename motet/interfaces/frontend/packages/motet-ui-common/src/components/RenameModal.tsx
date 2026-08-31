/**
 * Motet UI Common - Rename Modal
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     Generic modal dialog for renaming an entity (conversation, scope, etc.).
 *
 * Usage:
 *     import { RenameModal } from "@motet/ui-common/components";
 *
 *     <RenameModal
 *       open={show}
 *       onCancel={() => setShow(false)}
 *       onOk={handleSubmit}
 *       value={name}
 *       onChange={setName}
 *       title="Rename Conversation"
 *     />
 */
import { Form, Input, Modal } from "antd";

export interface RenameModalProps {
  /** Whether the modal is visible */
  open: boolean;
  /** Callback to close the modal without saving */
  onCancel: () => void;
  /** Callback to save the new name and close */
  onOk: () => void;
  /** Current name value in the input */
  value: string;
  /** Callback to update the name value */
  onChange: (value: string) => void;
  /** Modal title (default: "Rename") */
  title?: string;
  /** Input label (default: "Name") */
  label?: string;
  /** Input placeholder (default: "Enter name") */
  placeholder?: string;
}

export function RenameModal({
  open,
  onCancel,
  onOk,
  value,
  onChange,
  title = "Rename",
  label = "Name",
  placeholder = "Enter name",
}: RenameModalProps) {
  return (
    <Modal title={title} open={open} onOk={onOk} onCancel={onCancel}>
      <Form.Item label={label}>
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onPressEnter={onOk}
          placeholder={placeholder}
          autoFocus
        />
      </Form.Item>
    </Modal>
  );
}
