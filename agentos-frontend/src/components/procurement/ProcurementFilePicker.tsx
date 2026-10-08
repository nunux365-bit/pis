"use client";

import { useState, type Dispatch, type SetStateAction } from "react";
import {
  appendProcurementFilePicks,
  PROCUREMENT_ATTACHMENT_ACCEPT,
  readProcurementFileInput,
} from "@/lib/procurementApi";
import { PendingProcurementAttachments } from "@/components/procurement/PendingProcurementAttachments";

type Props = {
  id: string;
  files: File[];
  onChange: Dispatch<SetStateAction<File[]>>;
  disabled?: boolean;
  /** Shown above the staged file list. */
  caption?: string;
  inputClassName?: string;
  uploadLabel?: string;
};

/**
 * File input + staged attachment list. Reads the native picker synchronously so files are not
 * lost when React re-runs state updaters (e.g. Strict Mode).
 */
export function ProcurementFilePicker({
  id,
  files,
  onChange,
  disabled = false,
  caption,
  inputClassName,
  uploadLabel = "Attach files",
}: Props) {
  const [pickErr, setPickErr] = useState<string | null>(null);

  return (
    <>
      <label htmlFor={id} className="sr-only">
        {uploadLabel}
      </label>
      <input
        id={id}
        type="file"
        accept={PROCUREMENT_ATTACHMENT_ACCEPT}
        multiple
        disabled={disabled}
        className={inputClassName}
        onChange={(e) => {
          setPickErr(null);
          const picked = readProcurementFileInput(e.currentTarget);
          if (picked.length === 0) return;
          let errors: string[] = [];
          onChange((prev) => {
            const result = appendProcurementFilePicks(prev, picked);
            errors = result.errors;
            return result.files;
          });
          if (errors.length > 0) {
            setPickErr(errors.join(" "));
          }
        }}
      />
      {pickErr ? (
        <p className="mt-2 text-xs font-medium text-[var(--accent-red)]" role="alert">
          {pickErr}
        </p>
      ) : null}
      <PendingProcurementAttachments
        files={files}
        onChange={(next) => {
          setPickErr(null);
          onChange(next);
        }}
        caption={caption}
        disabled={disabled}
      />
    </>
  );
}
