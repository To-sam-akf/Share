import { beforeEach, describe, expect, it, vi } from "vitest";
import { fileContentUrl, saveSharedFile } from "./api";

describe("shared file downloads", () => {
  beforeEach(() => {
    delete (
      window as Window & { showSaveFilePicker?: unknown }
    ).showSaveFilePicker;
  });

  it("builds encoded content URLs", () => {
    expect(fileContentUrl("收到/报告 2026.pdf", "inline")).toBe(
      "/api/files/content?path=%E6%94%B6%E5%88%B0%2F%E6%8A%A5%E5%91%8A+2026.pdf&disposition=inline"
    );
  });

  it("streams the response into the selected file", async () => {
    const writable = {} as WritableStream<Uint8Array>;
    const pipeTo = vi.fn().mockResolvedValue(undefined);
    const picker = vi.fn().mockResolvedValue({
      createWritable: vi.fn().mockResolvedValue(writable)
    });
    Object.defineProperty(window, "showSaveFilePicker", {
      configurable: true,
      value: picker
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        body: { pipeTo }
      })
    );

    await expect(saveSharedFile("docs/report.pdf", "report.pdf")).resolves.toBe(
      "saved"
    );
    expect(picker).toHaveBeenCalledWith({ suggestedName: "report.pdf" });
    expect(pipeTo).toHaveBeenCalledWith(writable);
  });

  it("does nothing when the user cancels the picker", async () => {
    const picker = vi.fn().mockRejectedValue(
      new DOMException("cancelled", "AbortError")
    );
    Object.defineProperty(window, "showSaveFilePicker", {
      configurable: true,
      value: picker
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(saveSharedFile("docs/report.pdf", "report.pdf")).resolves.toBe(
      "cancelled"
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("falls back to a browser download when the picker is unavailable", async () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    await expect(saveSharedFile("docs/report.pdf", "report.pdf")).resolves.toBe(
      "downloaded"
    );
    expect(click).toHaveBeenCalledOnce();
  });

  it("reports server errors instead of silently falling back", async () => {
    Object.defineProperty(window, "showSaveFilePicker", {
      configurable: true,
      value: vi.fn().mockResolvedValue({
        createWritable: vi.fn()
      })
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: vi.fn().mockResolvedValue({ detail: "文件不存在" })
      })
    );

    await expect(
      saveSharedFile("docs/missing.pdf", "missing.pdf")
    ).rejects.toThrow("文件不存在");
  });
});
