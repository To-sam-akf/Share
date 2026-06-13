let csrfToken = "";

type FileDisposition = "inline" | "attachment";

interface SaveFileHandle {
  createWritable(): Promise<WritableStream<Uint8Array>>;
}

type SaveFilePicker = (options: {
  suggestedName: string;
}) => Promise<SaveFileHandle>;

type SaveSharedFileResult = "saved" | "downloaded" | "cancelled";

export async function initializeSession(): Promise<void> {
  const response = await fetch("/api/session", { credentials: "same-origin" });
  if (!response.ok) {
    throw new Error("无法初始化本地控制台会话。");
  }
  const payload = (await response.json()) as { csrf_token: string };
  csrfToken = payload.csrf_token;
}

export async function api<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof Blob) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (init.method && !["GET", "HEAD"].includes(init.method.toUpperCase())) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "same-origin"
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(payload?.detail || `请求失败 (${response.status})`);
  }
  return (await response.json()) as T;
}

export async function uploadFile(
  deviceId: string,
  file: File,
  onProgress?: (progress: number) => void
): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(
      "POST",
      `/api/transfers/upload?device_id=${encodeURIComponent(deviceId)}`
    );
    request.withCredentials = true;
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
    request.setRequestHeader("X-CSRF-Token", csrfToken);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.(Math.round((event.loaded * 100) / event.total));
      }
    };
    request.onerror = () => reject(new Error("文件上传失败，请检查本地服务。"));
    request.onload = () => {
      let payload: { detail?: string } = {};
      try {
        payload = request.responseText
          ? (JSON.parse(request.responseText) as { detail?: string })
          : {};
      } catch {
        payload = {};
      }
      if (request.status >= 200 && request.status < 300) {
        onProgress?.(100);
        resolve(payload);
      } else {
        reject(new Error(payload.detail || `请求失败 (${request.status})`));
      }
    };
    request.send(file);
  });
}

export function fileContentUrl(
  relativePath: string,
  disposition: FileDisposition
): string {
  const query = new URLSearchParams({
    path: relativePath,
    disposition
  });
  return `/api/files/content?${query.toString()}`;
}

export async function saveSharedFile(
  relativePath: string,
  fileName: string
): Promise<SaveSharedFileResult> {
  const picker = (
    window as Window & { showSaveFilePicker?: SaveFilePicker }
  ).showSaveFilePicker;
  const downloadUrl = fileContentUrl(relativePath, "attachment");

  if (picker) {
    try {
      const handle = await picker.call(window, { suggestedName: fileName });
      const response = await fetch(downloadUrl, {
        credentials: "same-origin"
      });
      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as {
          detail?: string;
        } | null;
        throw new Error(payload?.detail || `请求失败 (${response.status})`);
      }
      if (!response.body) {
        throw new Error("浏览器无法读取文件下载流。");
      }
      const writable = await handle.createWritable();
      await response.body.pipeTo(writable);
      return "saved";
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") {
        return "cancelled";
      }
      throw reason;
    }
  }

  const link = document.createElement("a");
  link.href = downloadUrl;
  link.download = fileName;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  return "downloaded";
}

export function eventSocket(): WebSocket {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(`${protocol}//${window.location.host}/api/events`);
}
