import { act, fireEvent, render, screen } from "@testing-library/react";
import { DocumentUploadModal, getDraggedFiles, getResultPresentation } from "./DocumentUploadModal";

jest.mock("@humansignal/core", () => ({ formatFileSize: (size) => `${size} bytes` }), { virtual: true });
jest.mock("@humansignal/icons", () => new Proxy({}, { get: () => () => null }), { virtual: true });
jest.mock(
  "@humansignal/ui",
  () => {
    const Container = ({ children, title }) => (
      <div>
        <div>{title}</div>
        <div>{children}</div>
      </div>
    );
    return {
      Badge: Container,
      Message: Container,
      SimpleCard: Container,
      Typography: Container,
      Button: ({ children, onClick, disabled, ...props }) => (
        <button onClick={onClick} disabled={disabled} aria-label={props["aria-label"]}>
          {children}
        </button>
      ),
    };
  },
  { virtual: true },
);
jest.mock("../../components/Form", () => ({ TextArea: (props) => <textarea {...props} /> }));
jest.mock("../../components/Modal/Modal", () => {
  const Modal = ({ children }) => <div>{children}</div>;
  Modal.Header = ({ children }) => <div>{children}</div>;
  return { Modal };
});
jest.mock("../../components/Space/Space", () => ({ Space: ({ children }) => <div>{children}</div> }));
jest.mock("../../providers/ProjectProvider", () => ({
  ProjectProvider: ({ children }) => children,
  useProject: () => ({ project: { id: 7 } }),
}));
jest.mock("../../providers/RoutesProvider", () => ({
  useFixedLocation: () => ({ pathname: "/projects/7/data/upload-documents", search: "" }),
}));
jest.mock("../../utils/hooks", () => ({ useRefresh: () => jest.fn() }));

const successful = { status: "success", tasks_created: 1 };
const failed = { status: "error", error: "Conversion failed" };
const response = (payload) => ({ ok: true, json: async () => payload });
let requests;
const chooseFiles = (...names) => {
  fireEvent.change(document.querySelector('input[type="file"]'), {
    target: { files: names.map((name) => new File(["text"], name)) },
  });
};
const finishUpload = async (payload, status = 202) => {
  const xhr = requests.at(-1);
  await act(async () => {
    xhr.status = status;
    xhr.responseText = JSON.stringify(payload);
    xhr.onload();
  });
};

beforeEach(() => {
  requests = [];
  window.APP_SETTINGS = { document_import: { allowed_extensions: [".txt", ".pdf"] } };
  global.fetch = jest.fn();
  jest.spyOn(window, "XMLHttpRequest").mockImplementation(() => {
    const xhr = {
      open: jest.fn(),
      setRequestHeader: jest.fn(),
      send: jest.fn(),
      upload: { addEventListener: jest.fn() },
    };
    requests.push(xhr);
    return xhr;
  });
});
afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

it("presents terminal and active result states", () => {
  expect(getResultPresentation(successful).label).toBe("Imported");
  expect(getResultPresentation(failed).label).toBe("Failed");
  expect(getResultPresentation({ status: "uploading" }).label).toBe("Uploading");
});

it("opens the file picker from its keyboard accessible browse button and honors configured formats", () => {
  render(<DocumentUploadModal />);
  const input = document.querySelector('input[type="file"]');
  const click = jest.spyOn(input, "click").mockImplementation(() => {});
  fireEvent.click(screen.getByRole("button", { name: "Browse documents to import" }));
  expect(click).toHaveBeenCalledTimes(1);
  expect(input.accept).toBe(".txt,.pdf");
});

it("allows retry after an initial upload failure without an import ID", async () => {
  render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ detail: "Queue unavailable" }, 503);
  fireEvent.click(screen.getByRole("button", { name: "Retry failed document imports" }));
  expect(requests).toHaveLength(2);
});

it("resumes polling a healthy import after a network error without submitting a retry job", async () => {
  jest.useFakeTimers();
  fetch.mockRejectedValueOnce(new Error("Network offline"));
  render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ import: 10, results: [{ status: "queued" }] });
  expect(screen.getByText("Resume Progress")).toBeTruthy();
  fetch
    .mockResolvedValueOnce(
      response({ status: "in_progress", job_health: "running", results: [{ status: "processing" }] }),
    )
    .mockResolvedValueOnce(response({ status: "completed", results: [successful] }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry failed document imports" })));
  expect(screen.getByText("Processing")).toBeTruthy();
  await act(async () => jest.advanceTimersByTime(1500));
  expect(screen.getByText("Imported 1 document")).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(fetch.mock.calls.every(([, options]) => options.method === "GET")).toBe(true);
});

it("waits for batch finalization even when all upload response rows succeeded", async () => {
  fetch.mockResolvedValueOnce(
    response({ status: "failed", has_errors: false, error: "Finalization failed", results: [successful] }),
  );
  render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ import: 10, results: [successful] });
  expect(await screen.findByText("Finalization failed")).toBeTruthy();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("keeps a successful original row when a subset retry needs job recovery", async () => {
  fetch.mockResolvedValueOnce(response({ status: "completed", results: [successful, failed] }));
  render(<DocumentUploadModal />);
  chooseFiles("first.txt", "second.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ import: 10, results: [successful, failed] });
  fireEvent.click(screen.getByRole("button", { name: "Retry failed document imports" }));
  fetch.mockResolvedValueOnce(
    response({
      status: "in_progress",
      job_health: "stalled",
      stalled_reason: "Worker stopped",
      results: [{ status: "queued" }],
    }),
  );
  await finishUpload({ import: 11, results: [{ status: "queued" }] });
  fetch
    .mockResolvedValueOnce(response({ import: 11, results: [{ status: "queued" }] }))
    .mockResolvedValueOnce(response({ status: "completed", results: [successful] }));
  fireEvent.click(screen.getByRole("button", { name: "Retry failed document imports" }));
  await screen.findByText("Imported 2 documents");
  expect(screen.getAllByText("Imported")).toHaveLength(2);
  expect(requests).toHaveLength(2);
});

it("rejects JSON metadata that is not an object", () => {
  render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Show advanced options" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Metadata JSON" }), { target: { value: "[]" } });
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  expect(screen.getByText("Metadata must be a valid JSON object before import can start.")).toBeTruthy();
  expect(requests).toHaveLength(0);
});

it("reads every directory batch and falls back to the native file list", async () => {
  const file = new File(["text"], "one.txt");
  const reader = {
    readEntries: jest
      .fn()
      .mockImplementationOnce((done) => done([{ name: file.name, isFile: true, file: (done) => done(file) }]))
      .mockImplementationOnce((done) => done([{ name: "two.txt", isFile: true, file: (done) => done(file) }]))
      .mockImplementationOnce((done) => done([])),
  };
  const directory = { name: "documents", isDirectory: true, createReader: () => reader };
  expect(await getDraggedFiles([{ webkitGetAsEntry: () => directory }], [])).toEqual([file, file]);
  expect(await getDraggedFiles([], [file])).toEqual([file]);
});

it("does not finish or lose the import when progress results are temporarily empty", async () => {
  fetch.mockResolvedValueOnce(response({ status: "in_progress", results: [] }));
  render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ import: 10, results: [{ status: "queued" }] });
  expect(screen.getByText("Resume Progress")).toBeTruthy();
  expect(screen.getByText(/incomplete import progress/)).toBeTruthy();
  fetch.mockResolvedValueOnce(response({ status: "completed", results: [successful] }));
  fireEvent.click(screen.getByRole("button", { name: "Retry failed document imports" }));
  await screen.findByText("Imported 1 document");
  expect(requests).toHaveLength(1);
});

it("ignores dropped files after completion until Import More starts a new batch", async () => {
  fetch.mockResolvedValueOnce(response({ status: "completed", results: [successful] }));
  render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ import: 10, results: [successful] });
  fireEvent.drop(document.querySelector('[class$="__canvas"]'), {
    dataTransfer: { items: [], files: [new File(["text"], "second.txt")] },
  });
  expect(screen.queryByText("second.txt")).toBeNull();
  expect(screen.getByText("Imported 1 document")).toBeTruthy();
});

it("rejects unreadable directory entries instead of leaving the drop operation pending", async () => {
  const entry = { name: "first.txt", isFile: true, file: (_done, reject) => reject(new Error("Unreadable")) };
  await expect(getDraggedFiles([{ webkitGetAsEntry: () => entry }], [])).rejects.toThrow("Unreadable");
});

it("does not start polling when an initial upload finishes after unmount", async () => {
  const { unmount } = render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  unmount();
  await finishUpload({ import: 10, results: [{ status: "queued" }] });
  expect(fetch).not.toHaveBeenCalled();
});

it("does not restart polling when a recovery POST finishes after unmount", async () => {
  fetch.mockResolvedValueOnce(
    response({ status: "in_progress", job_health: "stalled", results: [{ status: "queued" }] }),
  );
  const { unmount } = render(<DocumentUploadModal />);
  chooseFiles("first.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ import: 10, results: [{ status: "queued" }] });
  let resolveRetry;
  fetch.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolveRetry = resolve;
      }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Retry failed document imports" }));
  unmount();
  await act(async () => resolveRetry(response({ import: 10, results: [{ status: "queued" }] })));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("offers batch recovery when finalization fails alongside a per-file conversion error", async () => {
  fetch.mockResolvedValueOnce(
    response({ status: "failed", has_errors: true, error: "Finalization failed", results: [successful, failed] }),
  );
  render(<DocumentUploadModal />);
  chooseFiles("first.txt", "second.txt");
  fireEvent.click(screen.getByRole("button", { name: "Import selected documents" }));
  await finishUpload({ import: 10, results: [successful, failed] });
  expect(screen.getByText("Finalization failed")).toBeTruthy();
  fetch
    .mockResolvedValueOnce(response({ import: 10, results: [successful, failed] }))
    .mockResolvedValueOnce(response({ status: "completed", results: [successful, failed] }));
  fireEvent.click(screen.getByRole("button", { name: "Retry failed document imports" }));
  await screen.findByText("1 document imported, 1 need attention");
  expect(fetch.mock.calls[1][0]).toContain("/imports/10/retry-document-upload");
  expect(requests).toHaveLength(1);
});
