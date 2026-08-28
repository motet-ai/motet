/**
 * Motet - Chat Explorer - Rename Modal (re-export)
 *
 * Re-exports RenameModal from @motet/ui-common with conversation-specific defaults.
 */
import React from "react";
import { RenameModal as BaseRenameModal, type RenameModalProps as BaseProps } from "@motet/ui-common";

type RenameModalProps = Omit<BaseProps, "title" | "label" | "placeholder"> & {
  title?: string;
  label?: string;
  placeholder?: string;
};

export function RenameModal(props: RenameModalProps) {
  return (
    <BaseRenameModal
      title="Rename Conversation"
      label="Conversation Name"
      placeholder="Enter conversation name"
      {...props}
    />
  );
}
