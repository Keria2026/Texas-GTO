async function configurePanel(): Promise<void> {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

chrome.runtime.onInstalled.addListener(() => {
  void configurePanel().catch((error: unknown) => console.error("Side panel setup failed", error));
});
