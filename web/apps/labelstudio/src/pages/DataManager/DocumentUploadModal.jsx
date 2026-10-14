import { formatFileSize } from "@humansignal/core";
import {
  IconCheckCircleOutline,
  IconChevronDown,
  IconCloseCircleOutline,
  IconFileUpload,
  IconTrash,
  IconUpload,
  IconWarningCircle,
} from "@humansignal/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Message, SimpleCard, Typography } from "@humansignal/ui";
import { TextArea } from "../../components/Form";
import { Modal } from "../../components/Modal/Modal";
import { Space } from "../../components/Space/Space";
import { ProjectProvider, useProject } from "../../providers/ProjectProvider";
import { useFixedLocation } from "../../providers/RoutesProvider";
import { cn } from "../../utils/bem";
import { useRefresh } from "../../utils/hooks";
import "../CreateProject/Import/Import.prefix.css";
import "./DocumentUploadModal.prefix.css";

const uploadClass = cn("document-import");
const dropzoneClass = cn("dropzone");
const ACCEPTED_FILE_TYPES = ".txt,.md,.markdown";
const FALLBACK_ALLOWED_EXTENSIONS = ACCEPTED_FILE_TYPES.split(",");

// Directory readers return entries in batches, often 100 at a time.
async function traverseFileTree(item) {
  if (item.name.startsWith(".")) return [];
  if (item.isFile) return [await new Promise((resolve, reject) => item.file(resolve, reject))];
  if (!item.isDirectory) return [];
  const reader = item.createReader();
  const files = [];
  while (true) {
    const entries = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!entries.length) return files;
    files.push(...(await Promise.all(entries.map(traverseFileTree))).flat());
  }
}

export async function getDraggedFiles(items, fileList) {
  if (!items?.length || !items[0].webkitGetAsEntry) return Array.from(fileList ?? []);
  const entries = Array.from(items)
    .map((item) => item.webkitGetAsEntry())
    .filter(Boolean);
  return (await Promise.all(entries.map(traverseFileTree))).flat();
}

const collectMessages = (value) => {
  if (value === null || value === undefined || value === "") return [];
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(collectMessages);
  if (typeof value === "object") return Object.values(value).flatMap(collectMessages);
  return [String(value)];
};

const formatRequestError = (response) => {
  if (response?.status && [502, 503, 504].includes(response.status) && !response?.response) {
    return "The import service is temporarily unavailable. Check that Label Studio and its document worker are running, then try again.";
  }

  const detail = collectMessages(response?.response?.detail);
  if (detail.length) return detail.join(" ");

  const validationErrors = collectMessages(response?.response?.validation_errors);
  if (validationErrors.length) return validationErrors.join(" ");

  return response?.error ?? "Import failed.";
};

const getFileExtension = (filename) => {
  const parts = filename?.split(".");
  return parts?.length > 1 ? `.${parts.pop().toLowerCase()}` : "";
};

const createFileEntry = (file, index) => ({
  id: `${file.name}-${file.size}-${file.lastModified}-${index}-${Math.random().toString(36).slice(2, 8)}`,
  file,
  name: file.name,
  size: file.size,
  extension: getFileExtension(file.name),
});

const describeSelectionIssues = ({ unsupported, oversized, maxFileSize }) => {
  const messages = [];

  if (unsupported.length) {
    messages.push(
      `${unsupported.length} file${unsupported.length === 1 ? "" : "s"} were skipped because the format is not supported.`,
    );
  }

  if (oversized.length) {
    messages.push(
      `${oversized.length} file${oversized.length === 1 ? "" : "s"} were skipped because they exceed ${
        maxFileSize ? formatFileSize(maxFileSize) : "the current size limit"
      }.`,
    );
  }

  return messages.join(" ");
};

const getSummaryContent = ({ successCount, errorCount, taskCount }) => {
  if (successCount > 0 && errorCount === 0) {
    return {
      variant: "positive",
      title: `Imported ${successCount} document${successCount === 1 ? "" : "s"}`,
      description: `${taskCount} task${taskCount === 1 ? "" : "s"} created in this project.`,
    };
  }

  if (successCount > 0 && errorCount > 0) {
    return {
      variant: "warning",
      title: `${successCount} document${successCount === 1 ? "" : "s"} imported, ${errorCount} need attention`,
      description: `${taskCount} task${taskCount === 1 ? "" : "s"} created. Review the failed files below.`,
    };
  }

  return {
    variant: "negative",
    title: "No documents were imported",
    description: "Review the errors below, adjust your selection, and try again.",
  };
};

const getTypeBadgeVariant = (extension) => {
  if (extension === ".pdf") return "warning";
  if (extension === ".docx") return "neutral";
  return "primary";
};

export const getResultPresentation = (result) => {
  switch (result?.status) {
    case "success":
      return {
        label: "Imported",
        className: uploadClass.elem("result-success").toClassName(),
        icon: <IconCheckCircleOutline width={18} height={18} />,
      };
    case "error":
      return {
        label: "Failed",
        className: uploadClass.elem("result-error").toClassName(),
        icon: <IconCloseCircleOutline width={18} height={18} />,
      };
    case "processing":
      return {
        label: "Processing",
        className: uploadClass.elem("result-pending").toClassName(),
        icon: <IconUpload width={18} height={18} />,
      };
    case "uploading":
      return {
        label: "Uploading",
        className: uploadClass.elem("result-pending").toClassName(),
        icon: <IconUpload width={18} height={18} />,
      };
    case "queued":
      return {
        label: "Queued",
        className: uploadClass.elem("result-pending").toClassName(),
        icon: <IconUpload width={18} height={18} />,
      };
    default:
      return {
        label: "Ready",
        className: uploadClass.elem("result-idle").toClassName(),
        icon: <IconUpload width={18} height={18} />,
      };
  }
};

const getResultDetail = (result) => {
  switch (result?.status) {
    case "success":
      return `${result.tasks_created ?? 0} task${result.tasks_created === 1 ? "" : "s"} created`;
    case "uploading":
      return "Uploading with the current batch.";
    case "queued":
      return "Queued for preprocessing.";
    case "processing":
      return "Converting the document and creating tasks.";
    case "error":
      return result.error ?? "Import failed.";
    default:
      return "Ready to import";
  }
};

const getUploadBannerCopy = (batchUploadProgress) => {
  if (batchUploadProgress < 100) {
    return {
      title: `Uploading documents (${batchUploadProgress}%)`,
      description: "Your files are being transferred to Label Studio as one batch.",
    };
  }

  return {
    title: "Preprocessing documents",
    description: "Upload finished. Label Studio is converting the batch and creating tasks.",
  };
};

const getCsrfToken = () => document.querySelector("[name=csrfmiddlewaretoken]")?.value ?? "";

const getUploadEndpoint = (projectId) => {
  const hostname = (window.APP_SETTINGS?.hostname ?? window.location.origin).replace(/\/$/, "");

  return `${hostname}/api/projects/${projectId}/upload-files`;
};

const getImportStatusEndpoint = (projectId, importId) => {
  const hostname = (window.APP_SETTINGS?.hostname ?? window.location.origin).replace(/\/$/, "");

  return `${hostname}/api/projects/${projectId}/imports/${importId}/`;
};

const getRetryEndpoint = (projectId, importId) => {
  const hostname = (window.APP_SETTINGS?.hostname ?? window.location.origin).replace(/\/$/, "");

  return `${hostname}/api/projects/${projectId}/imports/${importId}/retry-document-upload`;
};

const uploadBatchWithXHR = ({ projectId, entries, metadata, onProgress, onProcessing }) =>
  new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    const formData = new FormData();
    const csrfToken = getCsrfToken();

    entries.forEach((entry) => {
      formData.append("files", entry.file);
    });

    if (metadata.trim()) {
      formData.append("metadata", metadata);
    }

    xhr.open("POST", getUploadEndpoint(projectId));
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");

    if (csrfToken) {
      xhr.setRequestHeader("X-CSRFToken", csrfToken);
    }

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;

      const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));

      if (percent >= 100) {
        onProcessing?.();
      } else {
        onProgress?.(percent);
      }
    });

    xhr.onerror = () => resolve({ error: "Network error while importing the file." });
    xhr.ontimeout = () => resolve({ error: "The upload request timed out." });

    xhr.onload = () => {
      if (xhr.status >= 400) {
        let payload = null;

        try {
          payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
        } catch {
          payload = null;
        }

        resolve({
          error: formatRequestError({
            status: xhr.status,
            response: payload,
            error: xhr.statusText || "Import failed.",
          }),
        });
        return;
      }

      let payload = null;

      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        resolve({ error: "Import failed because the server returned invalid JSON." });
        return;
      }

      resolve({ payload });
    };

    try {
      xhr.send(formData);
    } catch {
      resolve({ error: "The browser could not send the upload. Please try again." });
    }
  });

const postRetry = async ({ projectId, importId }) => {
  const csrfToken = getCsrfToken();
  const response = await fetch(getRetryEndpoint(projectId, importId), {
    method: "POST",
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(csrfToken ? { "X-CSRFToken": csrfToken } : {}),
    },
  });

  let payload = null;

  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    throw new Error(
      formatRequestError({
        status: response.status,
        response: payload,
        error: response.statusText || "Retry failed.",
      }),
    );
  }

  return payload;
};

const getImportStatus = async ({ projectId, importId }) => {
  const response = await fetch(getImportStatusEndpoint(projectId, importId), {
    method: "GET",
    credentials: "include",
    headers: {
      Accept: "application/json",
    },
  });

  let payload = null;

  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    throw new Error(
      formatRequestError({
        status: response.status,
        response: payload,
        error: response.statusText || "Failed to load import status.",
      }),
    );
  }

  return payload;
};

const Inner = () => {
  const refresh = useRefresh();
  const location = useFixedLocation();
  const { project } = useProject();
  const fileInputRef = useRef();
  const pollTokenRef = useRef(0);
  const mountedRef = useRef(true);
  const activeEntriesRef = useRef([]);
  const busyRef = useRef(false);
  const selectionLockedRef = useRef(false);
  const [recoveryAction, setRecoveryAction] = useState(null);
  const [hovered, setHovered] = useState(false);
  const [files, setFiles] = useState([]);
  const [metadata, setMetadata] = useState("");
  const [metadataExpanded, setMetadataExpanded] = useState(false);
  const [results, setResults] = useState([]);
  const [requestError, setRequestError] = useState("");
  const [selectionWarning, setSelectionWarning] = useState("");
  const [uploading, setUploading] = useState(false);
  const [batchFinished, setBatchFinished] = useState(false);
  const [activeImportId, setActiveImportId] = useState(null);
  const [batchUploadProgress, setBatchUploadProgress] = useState(0);

  const hasCompletedBatch = batchFinished;
  const runtimeSettings = useMemo(() => {
    const config = window.APP_SETTINGS?.document_import ?? {};

    return {
      maxFiles: config.max_files ?? null,
      maxFileSize: config.max_file_size ?? null,
      allowedExtensions: config.allowed_extensions?.length ? config.allowed_extensions : FALLBACK_ALLOWED_EXTENSIONS,
    };
  }, []);

  const duplicateNames = useMemo(() => {
    const counts = files.reduce((acc, file) => {
      acc.set(file.name, (acc.get(file.name) ?? 0) + 1);
      return acc;
    }, new Map());

    return [...counts.entries()].filter(([, count]) => count > 1).map(([name]) => name);
  }, [files]);

  const resultsByFileId = useMemo(
    () => new Map(results.filter((result) => result.fileId).map((result) => [result.fileId, result])),
    [results],
  );

  const failedEntries = useMemo(
    () => results.filter((result) => result.status === "error" && result.entry?.file).map((result) => result.entry),
    [results],
  );
  const canRetryImport = Boolean(failedEntries.length > 0 || (activeImportId && recoveryAction));

  const importSummary = useMemo(() => {
    const successCount = results.filter((result) => result.status === "success").length;
    const errorCount = results.filter((result) => result.status === "error").length;
    const taskCount = results.reduce((sum, result) => sum + (result.tasks_created ?? 0), 0);

    return {
      successCount,
      errorCount,
      taskCount,
      ...getSummaryContent({ successCount, errorCount, taskCount }),
    };
  }, [results]);

  const canSubmit = Boolean(project?.id) && files.length > 0 && !uploading && !hasCompletedBatch;

  const backToDM = useCallback(() => {
    const path = location.pathname.replace(DocumentUploadModal.path, "");
    const search = location.search;
    const pathname = `${path}${search !== "?" ? search : ""}`;

    return refresh(pathname);
  }, [location, refresh]);

  const onClose = useCallback(() => {
    if (uploading) return;
    backToDM();
  }, [backToDM, uploading]);

  const browseFiles = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const clearFeedback = useCallback(() => {
    setRequestError("");
    setSelectionWarning("");
    setResults([]);
    selectionLockedRef.current = false;
    setBatchFinished(false);
    setActiveImportId(null);
    setRecoveryAction(null);
    setBatchUploadProgress(0);
    pollTokenRef.current += 1;
  }, []);

  const updateResult = useCallback((fileId, updater) => {
    setResults((prev) => {
      const index = prev.findIndex((result) => result.fileId === fileId);
      const current = index >= 0 ? prev[index] : { fileId };
      const nextValue = typeof updater === "function" ? updater(current) : { ...current, ...updater };

      if (index >= 0) {
        const next = [...prev];
        next[index] = nextValue;
        return next;
      }

      return [...prev, nextValue];
    });
  }, []);

  const addFiles = useCallback(
    (incomingFiles) => {
      const incoming = Array.from(incomingFiles ?? []);
      if (!mountedRef.current || busyRef.current || selectionLockedRef.current || incoming.length === 0) return;

      const allowedExtensions = new Set(runtimeSettings.allowedExtensions);
      const unsupported = incoming.filter((file) => !allowedExtensions.has(getFileExtension(file.name)));
      const oversized =
        runtimeSettings.maxFileSize !== null ? incoming.filter((file) => file.size > runtimeSettings.maxFileSize) : [];
      const accepted = incoming.filter(
        (file) =>
          allowedExtensions.has(getFileExtension(file.name)) &&
          (runtimeSettings.maxFileSize === null || file.size <= runtimeSettings.maxFileSize),
      );

      clearFeedback();

      if (runtimeSettings.maxFiles && files.length + accepted.length > runtimeSettings.maxFiles) {
        setRequestError(
          `You can import up to ${runtimeSettings.maxFiles} file${
            runtimeSettings.maxFiles === 1 ? "" : "s"
          } at once. Remove some files before adding more.`,
        );
        return;
      }

      if (unsupported.length || oversized.length) {
        setSelectionWarning(
          describeSelectionIssues({
            unsupported,
            oversized,
            maxFileSize: runtimeSettings.maxFileSize,
          }),
        );
      }

      if (accepted.length === 0) return;

      setFiles((prev) => [...prev, ...accepted.map((file, index) => createFileEntry(file, prev.length + index))]);
    },
    [clearFeedback, files.length, runtimeSettings],
  );

  const removeFile = useCallback(
    (fileId) => {
      clearFeedback();
      setFiles((prev) => prev.filter((file) => file.id !== fileId));
      setResults((prev) => prev.filter((result) => result.fileId !== fileId));
    },
    [clearFeedback],
  );

  const clearAll = useCallback(() => {
    clearFeedback();
    setFiles([]);
  }, [clearFeedback]);

  const importMore = useCallback(() => {
    clearFeedback();
    setFiles([]);
  }, [clearFeedback]);

  const applyServerResults = useCallback(
    (entriesToUpload, serverResults) => {
      entriesToUpload.forEach((entry, index) => {
        const result = serverResults?.[index];

        updateResult(entry.id, {
          entry,
          fileId: entry.id,
          filename: entry.name,
          status: result?.status ?? "queued",
          tasks_created: result?.tasks_created ?? 0,
          task_ids: result?.task_ids ?? [],
          artifact_id: result?.artifact_id,
          error: result?.error ?? "",
          uploadProgress: 100,
        });
      });
    },
    [updateResult],
  );

  const pollImport = useCallback(
    async (importId, entriesToUpload) => {
      if (!mountedRef.current) return;
      const token = ++pollTokenRef.current;

      while (pollTokenRef.current === token) {
        try {
          const payload = await getImportStatus({ projectId: project.id, importId });

          if (pollTokenRef.current !== token) return;
          if (!Array.isArray(payload?.results) || payload.results.length !== entriesToUpload.length) {
            throw new Error("The server returned incomplete import progress. Resume to check the batch again.");
          }
          applyServerResults(entriesToUpload, payload.results);

          if (payload?.job_health === "stalled" || payload?.job_health === "failed") {
            setRecoveryAction("retry");
            busyRef.current = false;
            setRequestError(payload?.stalled_reason ?? "The background import job needs recovery.");
            setUploading(false);
            setBatchFinished(true);
            return;
          }

          if (
            payload?.status === "failed" &&
            (!payload?.has_errors || payload.results.some((result) => result.status === "success"))
          ) {
            setRecoveryAction("retry");
            setRequestError(payload?.error ?? "The background import failed before the batch could finish.");
          }

          const terminal = payload?.status === "completed" || payload?.status === "failed";

          if (terminal) {
            busyRef.current = false;
            setUploading(false);
            setBatchFinished(true);
            return;
          }
        } catch (error) {
          if (pollTokenRef.current !== token) return;
          busyRef.current = false;
          setRecoveryAction("poll");
          setRequestError(error.message || "Failed to poll document import progress.");
          setUploading(false);
          setBatchFinished(true);
          return;
        }

        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      }
    },
    [applyServerResults, project?.id],
  );

  const submitEntries = useCallback(
    async (entries) => {
      const entriesToUpload = entries?.length ? entries : files;
      if (!project?.id || entriesToUpload.length === 0 || busyRef.current) return;

      if (metadata.trim()) {
        try {
          const value = JSON.parse(metadata);
          if (value === null || Array.isArray(value) || typeof value !== "object") throw new Error();
        } catch {
          setRequestError("Metadata must be a valid JSON object before import can start.");
          return;
        }
      }

      setRequestError("");
      setSelectionWarning("");
      setBatchFinished(false);
      setActiveImportId(null);
      setBatchUploadProgress(0);

      busyRef.current = true;
      selectionLockedRef.current = true;
      activeEntriesRef.current = entriesToUpload;
      setRecoveryAction(null);
      setUploading(true);
      const retriedIds = new Set(entriesToUpload.map((entry) => entry.id));
      setResults((prev) => prev.filter((result) => !retriedIds.has(result.fileId)));

      entriesToUpload.forEach((entry) => {
        updateResult(entry.id, {
          entry,
          fileId: entry.id,
          filename: entry.name,
          status: "queued",
          tasks_created: 0,
          task_ids: [],
          error: "",
          uploadProgress: 0,
        });
      });

      const setBatchStatus = (statusValue) => {
        entriesToUpload.forEach((entry) => {
          updateResult(entry.id, (current) => ({
            ...current,
            status: statusValue,
            uploadProgress: statusValue === "uploading" ? batchUploadProgress : 100,
          }));
        });
      };

      setBatchStatus("uploading");
      const requestToken = pollTokenRef.current;
      const isCurrentRequest = () => mountedRef.current && pollTokenRef.current === requestToken;

      const { payload, error } = await uploadBatchWithXHR({
        projectId: project.id,
        entries: entriesToUpload,
        metadata,
        onProgress: (percent) => {
          if (!isCurrentRequest()) return;
          setBatchUploadProgress(percent);
          entriesToUpload.forEach((entry) => {
            updateResult(entry.id, (current) => ({
              ...current,
              status: "uploading",
              uploadProgress: percent,
            }));
          });
        },
        onProcessing: () => {
          if (!isCurrentRequest()) return;
          setBatchUploadProgress(100);
          setBatchStatus("processing");
        },
      });

      if (!isCurrentRequest()) return;
      if (error) {
        busyRef.current = false;
        entriesToUpload.forEach((entry) => {
          updateResult(entry.id, (current) => ({
            ...current,
            status: "error",
            error,
            uploadProgress: 100,
          }));
        });
        setUploading(false);
        setBatchFinished(true);
        return;
      }

      if (!Array.isArray(payload?.results) || payload.results.length !== entriesToUpload.length) {
        if (payload?.import) {
          setActiveImportId(payload.import);
          setBatchUploadProgress(100);
          await pollImport(payload.import, entriesToUpload);
          return;
        }
        busyRef.current = false;
        entriesToUpload.forEach((entry) => {
          updateResult(entry.id, (current) => ({
            ...current,
            status: "error",
            error: "Import failed because the server returned no per-file result.",
            uploadProgress: 100,
          }));
        });
        setUploading(false);
        setBatchFinished(true);
        return;
      }

      setBatchUploadProgress(100);
      applyServerResults(entriesToUpload, payload.results);
      setActiveImportId(payload.import ?? null);

      if (!payload.import) {
        busyRef.current = false;
        setUploading(false);
        setBatchFinished(true);
        return;
      }

      await pollImport(payload.import, entriesToUpload);
    },
    [applyServerResults, batchUploadProgress, files, metadata, pollImport, project?.id, updateResult, uploading],
  );

  const onUpload = useCallback(() => submitEntries(files), [files, submitEntries]);

  const retryFailed = useCallback(async () => {
    if (busyRef.current) return;
    if (activeImportId && project?.id && recoveryAction) {
      busyRef.current = true;
      setUploading(true);
      setRequestError("");
      setBatchFinished(false);
      setBatchUploadProgress(100);
      const requestToken = pollTokenRef.current;
      const isCurrentRequest = () => mountedRef.current && pollTokenRef.current === requestToken;
      try {
        if (recoveryAction === "retry") {
          const payload = await postRetry({ projectId: project.id, importId: activeImportId });
          if (!isCurrentRequest()) return;
          applyServerResults(activeEntriesRef.current, payload?.results ?? []);
        }
        setRecoveryAction(null);
        await pollImport(activeImportId, activeEntriesRef.current);
      } catch (error) {
        if (!isCurrentRequest()) return;
        busyRef.current = false;
        // A lost response can follow a successful retry; check status before retrying again.
        setRecoveryAction("poll");
        setRequestError(error.message || "Retry failed.");
        setUploading(false);
        setBatchFinished(true);
      }
      return;
    }
    if (failedEntries.length) await submitEntries(failedEntries);
  }, [activeImportId, applyServerResults, failedEntries, pollImport, project?.id, recoveryAction, submitEntries]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      pollTokenRef.current += 1;
    };
  }, []);

  const onDrop = useCallback(
    (event) => {
      event.preventDefault();
      setHovered(false);
      if (busyRef.current || batchFinished) return;
      getDraggedFiles(event.dataTransfer.items, event.dataTransfer.files)
        .then(addFiles)
        .catch(() => setSelectionWarning("Dropped files could not be read. Try selecting them with Browse Documents."));
    },
    [addFiles, batchFinished],
  );

  const onInputChange = useCallback(
    (event) => {
      addFiles(event.target.files);
      event.target.value = "";
    },
    [addFiles],
  );

  const selectionCountLabel = runtimeSettings.maxFiles
    ? `${files.length} of ${runtimeSettings.maxFiles} file${runtimeSettings.maxFiles === 1 ? "" : "s"} selected`
    : `${files.length} file${files.length === 1 ? "" : "s"} selected`;

  const batchLimitLabel = runtimeSettings.maxFileSize
    ? `Up to ${formatFileSize(runtimeSettings.maxFileSize)} per file`
    : "Project document limits apply";

  const uploadBanner = getUploadBannerCopy(batchUploadProgress);

  return (
    <Modal title="Import documents" onHide={onClose} closeOnClickOutside={false} fullscreen visible bare>
      <Modal.Header divided>
        <div className={uploadClass.elem("title").toClassName()}>Import Documents</div>
        <Space>
          <Button
            size="small"
            look="outlined"
            onClick={onClose}
            disabled={uploading}
            aria-label="Close document import"
          >
            {hasCompletedBatch ? "Done" : "Close"}
          </Button>
          <Button
            size="small"
            onClick={onUpload}
            waiting={uploading}
            disabled={!canSubmit}
            leading={<IconUpload />}
            aria-label="Import selected documents"
          >
            Import Documents
          </Button>
        </Space>
      </Modal.Header>

      <div className={uploadClass}>
        <input
          id="document-upload-input"
          ref={fileInputRef}
          type="file"
          multiple
          accept={runtimeSettings.allowedExtensions.join(",")}
          onChange={onInputChange}
          disabled={uploading}
          style={{ display: "none" }}
        />

        {duplicateNames.length > 0 && (
          <div className={uploadClass.elem("banner").toClassName()}>
            <Message variant="warning" title="Some files share the same name">
              They will still be imported separately. Review the list before you continue.
            </Message>
          </div>
        )}

        {selectionWarning && (
          <div className={uploadClass.elem("banner").toClassName()}>
            <Message variant="warning" title="Some files were not added">
              {selectionWarning}
            </Message>
          </div>
        )}

        {requestError && (
          <div className={uploadClass.elem("banner").toClassName()}>
            <Message variant="negative" title="Import needs attention">
              {requestError}
            </Message>
          </div>
        )}

        <div
          className={uploadClass.elem("canvas").toClassName()}
          onDragStart={(event) => {
            event.preventDefault();
            setHovered(true);
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setHovered(true);
          }}
          onDragLeave={() => setHovered(false)}
          onDrop={onDrop}
        >
          <div className={uploadClass.elem("layout").toClassName()}>
            {uploading && (
              <div className={uploadClass.elem("banner").toClassName()} aria-live="polite">
                <Message variant="primary" title={uploadBanner.title}>
                  <div className={uploadClass.elem("message-content").toClassName()}>
                    <span>{uploadBanner.description}</span>
                    <div className={uploadClass.elem("batch-progress").toClassName()}>
                      <div
                        className={uploadClass.elem("batch-progress-bar").toClassName()}
                        style={{ width: `${batchUploadProgress}%` }}
                      />
                    </div>
                  </div>
                </Message>
              </div>
            )}

            {files.length === 0 ? (
              <div className={dropzoneClass.mod({ hovered }).toClassName()}>
                <label htmlFor="document-upload-input" className={uploadClass.elem("dropzone-label").toClassName()}>
                  <div
                    className={`${dropzoneClass.elem("content").toClassName()} ${uploadClass
                      .elem("dropzone-content")
                      .toClassName()}`}
                  >
                    <IconFileUpload height="64" className={dropzoneClass.elem("icon").toClassName()} />
                    <header>Drag and drop documents here or click to browse</header>
                    <Typography variant="body" className={uploadClass.elem("helper-note").toClassName()}>
                      Imported files are preprocessed into project tasks using the current labeling configuration.
                    </Typography>
                    <div className={uploadClass.elem("support-list").toClassName()}>
                      {runtimeSettings.allowedExtensions.map((extension) => (
                        <Badge key={extension} size="small" variant={getTypeBadgeVariant(extension)}>
                          {extension.replace(".", "").toUpperCase()}
                        </Badge>
                      ))}
                    </div>
                    <div className={uploadClass.elem("limits").toClassName()}>
                      <Typography variant="body" size="small">
                        {runtimeSettings.maxFiles
                          ? `Up to ${runtimeSettings.maxFiles} files per batch`
                          : "Batch import available"}
                      </Typography>
                      <Typography variant="body" size="small">
                        {batchLimitLabel}
                      </Typography>
                    </div>
                    <Button onClick={browseFiles} leading={<IconUpload />} aria-label="Browse documents to import">
                      Browse Documents
                    </Button>
                  </div>
                </label>
              </div>
            ) : (
              <>
                {!hasCompletedBatch && (
                  <div
                    className={uploadClass.elem("inline-dropzone").mod({ hovered }).toClassName()}
                    role="presentation"
                  >
                    <div
                      className={`${dropzoneClass.elem("content").toClassName()} ${uploadClass
                        .elem("inline-dropzone-content")
                        .toClassName()}`}
                    >
                      <div className={uploadClass.elem("inline-dropzone-copy").toClassName()}>
                        <Typography variant="label">Add more documents</Typography>
                        <Typography
                          variant="body"
                          size="small"
                          className={uploadClass.elem("helper-note").toClassName()}
                        >
                          Drop more files anywhere in this area or browse to extend the current batch.
                        </Typography>
                      </div>
                      <Button
                        look="outlined"
                        onClick={browseFiles}
                        leading={<IconUpload />}
                        disabled={uploading}
                        aria-label="Browse more documents"
                      >
                        Add Files
                      </Button>
                    </div>
                  </div>
                )}

                {hasCompletedBatch && (
                  <SimpleCard
                    title="Latest batch"
                    description="Review the outcome before returning to Data Manager."
                    className="bg-transparent"
                  >
                    <div className={uploadClass.elem("summary-card-content").toClassName()} aria-live="polite">
                      <Message variant={importSummary.variant} title={importSummary.title}>
                        {importSummary.description}
                      </Message>
                      <div className={uploadClass.elem("summary-actions").toClassName()}>
                        {canRetryImport && (
                          <Button
                            look="outlined"
                            onClick={retryFailed}
                            disabled={uploading}
                            leading={<IconWarningCircle />}
                            aria-label="Retry failed document imports"
                          >
                            {recoveryAction === "poll" ? "Resume Progress" : "Retry Failed"}
                          </Button>
                        )}
                        <Button
                          look="outlined"
                          variant="neutral"
                          onClick={importMore}
                          disabled={uploading}
                          leading={<IconUpload />}
                          aria-label="Start a new document import batch"
                        >
                          Import More
                        </Button>
                      </div>
                      <Typography
                        variant="body"
                        size="small"
                        className={uploadClass.elem("results-note").toClassName()}
                      >
                        Retry only works while these files remain available in this browser session.
                      </Typography>
                    </div>
                  </SimpleCard>
                )}

                <SimpleCard
                  title={
                    <div className={uploadClass.elem("files-heading").toClassName()}>
                      <span>Selected documents</span>
                      <Badge variant="neutral" size="small">
                        {selectionCountLabel}
                      </Badge>
                    </div>
                  }
                  description="Files are imported into the current project and turned into normal Label Studio tasks."
                >
                  <div className={uploadClass.elem("toolbar").toClassName()}>
                    <div className={uploadClass.elem("toolbar-meta").toClassName()}>
                      <Typography variant="label">{selectionCountLabel}</Typography>
                      <Typography variant="body" size="small" className={uploadClass.elem("helper-note").toClassName()}>
                        {batchLimitLabel}
                      </Typography>
                    </div>
                    {!hasCompletedBatch && (
                      <div className={uploadClass.elem("toolbar-actions").toClassName()}>
                        <Button
                          look="outlined"
                          onClick={browseFiles}
                          disabled={uploading}
                          leading={<IconUpload />}
                          aria-label="Browse additional documents"
                        >
                          Add Files
                        </Button>
                        <Button
                          look="string"
                          variant="neutral"
                          onClick={clearAll}
                          disabled={uploading}
                          aria-label="Clear all selected documents"
                        >
                          Clear All
                        </Button>
                      </div>
                    )}
                  </div>

                  <div className={uploadClass.elem("file-table-wrapper").toClassName()}>
                    <table className={uploadClass.elem("file-table").toClassName()} aria-label="Selected documents">
                      <thead>
                        <tr>
                          <th>Filename</th>
                          <th>Type</th>
                          <th>Size</th>
                          <th>Status</th>
                          <th scope="col">Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {files.map((entry) => {
                          const result = resultsByFileId.get(entry.id);
                          const statusPresentation = getResultPresentation(result);

                          return (
                            <tr key={entry.id}>
                              <td>
                                <div className={uploadClass.elem("file-name-cell").toClassName()}>
                                  <div className={uploadClass.elem("file-name").toClassName()} title={entry.name}>
                                    {entry.name}
                                  </div>
                                  <div className={uploadClass.elem("file-subtitle").toClassName()}>
                                    {getResultDetail(result)}
                                  </div>
                                </div>
                              </td>
                              <td>
                                <Badge variant={getTypeBadgeVariant(entry.extension)} size="small">
                                  {entry.extension.replace(".", "").toUpperCase() || "FILE"}
                                </Badge>
                              </td>
                              <td className={uploadClass.elem("file-size").toClassName()}>
                                {formatFileSize(entry.size)}
                              </td>
                              <td>
                                <div
                                  className={`${uploadClass.elem("row-status").toClassName()} ${statusPresentation.className}`}
                                >
                                  {statusPresentation.icon}
                                  <span>{statusPresentation.label}</span>
                                </div>
                              </td>
                              <td className={uploadClass.elem("actions-cell").toClassName()}>
                                <Button
                                  look="string"
                                  variant="neutral"
                                  size="smaller"
                                  onClick={() => removeFile(entry.id)}
                                  disabled={uploading || hasCompletedBatch}
                                  leading={<IconTrash />}
                                  aria-label={`Remove ${entry.name}`}
                                />
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </SimpleCard>

                <SimpleCard
                  title="Advanced options"
                  description="Optional metadata applied to each uploaded source file."
                >
                  <div className={uploadClass.elem("metadata-panel").toClassName()}>
                    <Button
                      look="string"
                      align="left"
                      className={uploadClass.elem("metadata-toggle").toClassName()}
                      onClick={() => setMetadataExpanded((value) => !value)}
                      trailing={
                        <IconChevronDown
                          width={16}
                          height={16}
                          className={uploadClass.elem("chevron").mod({ expanded: metadataExpanded }).toClassName()}
                        />
                      }
                      aria-expanded={metadataExpanded}
                      aria-label={metadataExpanded ? "Hide advanced options" : "Show advanced options"}
                    >
                      {metadataExpanded ? "Hide Metadata" : "Add Metadata"}
                    </Button>
                    {metadataExpanded && (
                      <TextArea
                        label=""
                        rows={6}
                        value={metadata}
                        onChange={(event) => setMetadata(event.target.value)}
                        placeholder='{"source":"internal"}'
                        disabled={uploading || hasCompletedBatch}
                        aria-label="Metadata JSON"
                      />
                    )}
                    <Typography variant="body" size="small" className={uploadClass.elem("helper-note").toClassName()}>
                      Use a single JSON object. TXT and Markdown files pass through directly. PDF and DOCX use the
                      configured converter service.
                    </Typography>
                  </div>
                </SimpleCard>
              </>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
};

export const DocumentUploadModal = () => {
  return (
    <ProjectProvider>
      <Inner />
    </ProjectProvider>
  );
};

DocumentUploadModal.path = "/upload-documents";
DocumentUploadModal.modal = true;
