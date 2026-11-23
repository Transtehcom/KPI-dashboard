using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: AssemblyTitle("Дашборд Транстехком")]
[assembly: AssemblyCompany("Транстехком")]
[assembly: AssemblyProduct("Дашборд Транстехком")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

internal sealed class Dashboard : Form
{
    private static readonly Uri Home = new Uri("https://kpi.transtehcom.ru:8443/");
    private readonly WebView2 browser = new WebView2 { Dock = DockStyle.Fill };
    private readonly FlowLayoutPanel status = new FlowLayoutPanel {
        Dock = DockStyle.Top, AutoSize = true, Padding = new Padding(12)
    };
    private readonly Label message = new Label { AutoSize = true, Margin = new Padding(2, 8, 10, 4) };
    private readonly Button retry = new Button { Text = "Повторить", AutoSize = true, Visible = false };

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Dashboard());
    }

    private Dashboard()
    {
        Text = "Дашборд Транстехком";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        StartPosition = FormStartPosition.CenterScreen;
        Rectangle screen = Screen.FromControl(this).WorkingArea;
        Size = new Size(Math.Min(1320, screen.Width), Math.Min(860, screen.Height));
        Controls.Add(browser);
        Controls.Add(status);
        status.Controls.Add(message);
        status.Controls.Add(retry);
        message.Text = "Открываем дашборд…";
        retry.Click += delegate { OpenDashboard(); };
        Shown += delegate { OpenDashboard(); };
    }

    private async void OpenDashboard()
    {
        retry.Visible = false;
        status.Visible = true;
        message.Text = "Открываем дашборд…";
        try
        {
            if (browser.CoreWebView2 == null)
            {
                string profile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "Transtehcom", "Dashboard", "WebView2");
                var environment = await CoreWebView2Environment.CreateAsync(null, profile);
                await browser.EnsureCoreWebView2Async(environment);
                var core = browser.CoreWebView2;
                core.Settings.AreHostObjectsAllowed = false;
                core.Settings.IsWebMessageEnabled = false;
                core.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs e) {
                    if (e.Uri == "about:blank" || IsDashboard(e.Uri)) return;
                    e.Cancel = true;
                    if (e.IsUserInitiated) OpenExternal(e.Uri);
                };
                core.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e) {
                    e.Handled = true;
                    if (e.IsUserInitiated)
                    {
                        if (IsDashboard(e.Uri)) core.Navigate(e.Uri);
                        else OpenExternal(e.Uri);
                    }
                };
                core.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs e) {
                    if (e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled) return;
                    if (e.IsSuccess)
                    {
                        status.Visible = false;
                        browser.Focus();
                    }
                    else ShowError("Не удалось открыть дашборд. Проверьте подключение к сети.");
                };
            }
            browser.CoreWebView2.Navigate(Home.AbsoluteUri);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            if (MessageBox.Show(this, "Для приложения требуется Microsoft Edge WebView2 Runtime. Открыть страницу установки?",
                Text, MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
                OpenExternal("https://developer.microsoft.com/microsoft-edge/webview2/");
            ShowError("Установите Microsoft Edge WebView2 Runtime и повторите попытку.");
        }
        catch (Exception ex)
        {
            if (!IsDisposed) ShowError("Не удалось открыть дашборд. " + ex.Message);
        }
    }

    private static bool IsDashboard(string value)
    {
        Uri uri;
        return Uri.TryCreate(value, UriKind.Absolute, out uri) && uri.Scheme == Home.Scheme
            && uri.Host == Home.Host && uri.Port == Home.Port && uri.UserInfo.Length == 0;
    }

    private void OpenExternal(string value)
    {
        Uri uri;
        if (!Uri.TryCreate(value, UriKind.Absolute, out uri)
            || (uri.Scheme != Uri.UriSchemeHttps && uri.Scheme != Uri.UriSchemeHttp)) return;
        try { Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true }); }
        catch (System.ComponentModel.Win32Exception) { ShowError("Не удалось открыть ссылку в браузере."); }
    }

    private void ShowError(string text)
    {
        message.Text = text;
        status.Visible = true;
        retry.Visible = true;
    }
}
