// Single-file installer for ILTC WEEKLY SCHEDULE.
//
// The app itself is a Python/pywebview build, so it cannot be one small
// executable the way HoneNotes is - it needs its runtime next to it. This
// wraps the whole build into one file you can send someone: they run it,
// it unpacks into %LOCALAPPDATA%\Programs\ILTC WEEKLY SCHEDULE, makes a
// Start menu shortcut, and opens the app.
//
// Built with the C# compiler that ships with Windows, so there is nothing
// extra to install - see build-installer.ps1.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

static class Installer
{
    const string AppName = "ILTC WEEKLY SCHEDULE";
    const string ExeName = "ILTC WEEKLY SCHEDULE.exe";

    // Written by the app, not by us. Never overwritten on an upgrade -
    // losing someone's creator codes to an update would be unforgivable.
    static readonly string[] UserData = {
        "schedule_config.json", "schedule_gui.log", "schedule_gui.log.1",
        "schedule_panel_log.jsonl",
    };

    static readonly string LogPath = Path.Combine(Path.GetTempPath(), "iltc_schedule_setup.log");

    static void Log(string message)
    {
        try
        {
            File.AppendAllText(LogPath,
                DateTime.Now.ToString("HH:mm:ss") + "  " + message + Environment.NewLine);
        }
        catch { }
    }

    [STAThread]
    static int Main()
    {
        // Anything thrown before the try below - loading a missing
        // assembly, say - would otherwise kill the process with no
        // message at all, which is exactly what a recipient must not get.
        AppDomain.CurrentDomain.UnhandledException += (s, e) => {
            Log("UNHANDLED: " + e.ExceptionObject);
            try {
                MessageBox.Show("Could not install " + AppName + ".\n\n" + e.ExceptionObject +
                    "\n\nDetails: " + LogPath, AppName,
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            } catch { }
        };

        Log("--- setup start ---");
        Application.EnableVisualStyles();
        string installDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs", AppName);
        Log("install dir: " + installDir);

        Form splash = null;
        try
        {
            // Inside the try: a failure building the splash used to take
            // the process down with no message.
            splash = ShowSplash();
            CloseRunningCopies();
            Log("closed running copies");
            Extract(installDir);
            Log("extracted");
            CreateShortcut(installDir);
            if (splash != null) splash.Close();

            string exe = Path.Combine(installDir, ExeName);
            Log("starting " + exe);
            Process.Start(new ProcessStartInfo(exe) { WorkingDirectory = installDir });
            Log("--- done ---");
            return 0;
        }
        catch (Exception ex)
        {
            Log("FAILED: " + ex);
            try { if (splash != null) splash.Close(); } catch { }
            MessageBox.Show(
                "Could not install " + AppName + ".\n\n" + ex.Message +
                "\n\nIf it is already open, close it and run this again." +
                "\n\nDetails: " + LogPath,
                AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    static Form ShowSplash()
    {
        // Unpacking ~100 MB takes a few seconds; without this people
        // double-click again and race two installs against each other.
        Form form = new Form {
            Text = AppName,
            FormBorderStyle = FormBorderStyle.FixedDialog,
            StartPosition = FormStartPosition.CenterScreen,
            ClientSize = new Size(360, 90),
            MaximizeBox = false, MinimizeBox = false, ControlBox = false,
            BackColor = ColorTranslator.FromHtml("#111318"),
        };
        form.Controls.Add(new Label {
            Text = "Installing " + AppName + "...",
            ForeColor = ColorTranslator.FromHtml("#f3f4f6"),
            Font = new Font("Segoe UI", 10F),
            AutoSize = false, Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
        });
        form.Show();
        Application.DoEvents();
        return form;
    }

    static void CloseRunningCopies()
    {
        // Exact names, and never this process. This installer is called
        // "ILTC WEEKLY SCHEDULE Setup", so a StartsWith test matched
        // itself and the installer killed itself before unpacking
        // anything - exit code -1, no error, nothing installed.
        int self = Process.GetCurrentProcess().Id;
        foreach (Process p in Process.GetProcesses())
        {
            string name;
            try
            {
                if (p.Id == self) continue;
                name = p.ProcessName;
            }
            catch { continue; }

            if (!name.Equals("ILTC WEEKLY SCHEDULE", StringComparison.OrdinalIgnoreCase) &&
                !name.Equals("ILTC WEEKLY SCHEDULE WORKER", StringComparison.OrdinalIgnoreCase))
                continue;

            Log("closing " + name + " (pid " + p.Id + ")");
            try { p.Kill(); p.WaitForExit(5000); } catch { }
        }
        // Windows can hold the file handles open a moment longer.
        Thread.Sleep(600);
    }

    static void Extract(string installDir)
    {
        Directory.CreateDirectory(installDir);
        Assembly self = Assembly.GetExecutingAssembly();

        using (Stream payload = self.GetManifestResourceStream("Payload.zip"))
        {
            if (payload == null) throw new Exception("This installer is missing its payload.");
            using (ZipArchive zip = new ZipArchive(payload, ZipArchiveMode.Read))
            {
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    // Entries are "ILTC WEEKLY SCHEDULE/..." - strip that
                    // so the contents land in installDir itself.
                    string relative = entry.FullName.Replace('/', '\\');
                    int slash = relative.IndexOf('\\');
                    if (slash >= 0) relative = relative.Substring(slash + 1);
                    if (relative.Length == 0) continue;

                    string target = Path.Combine(installDir, relative);
                    if (entry.Length == 0 && relative.EndsWith("\\"))
                    {
                        Directory.CreateDirectory(target);
                        continue;
                    }
                    // Never overwrite the user's own settings/log on an update;
                    // the starter settings only go in on a first install.
                    if (IsUserData(relative) && File.Exists(target)) continue;

                    Directory.CreateDirectory(Path.GetDirectoryName(target));
                    entry.ExtractToFile(target, true);
                }
            }
        }
    }

    static bool IsUserData(string relative)
    {
        if (relative.IndexOf('\\') >= 0) return false;   // only top level
        foreach (string name in UserData)
            if (string.Equals(relative, name, StringComparison.OrdinalIgnoreCase))
                return true;
        return false;
    }

    static void CreateShortcut(string installDir)
    {
        // WScript.Shell through late binding, so this needs no COM
        // reference at build time.
        try
        {
            string startMenu = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                AppName + ".lnk");
            Type shellType = Type.GetTypeFromProgID("WScript.Shell");
            if (shellType == null) return;
            object shell = Activator.CreateInstance(shellType);
            object link = shellType.InvokeMember("CreateShortcut",
                BindingFlags.InvokeMethod, null, shell, new object[] { startMenu });
            Type linkType = link.GetType();
            linkType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link,
                new object[] { Path.Combine(installDir, ExeName) });
            linkType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link,
                new object[] { installDir });
            linkType.InvokeMember("Description", BindingFlags.SetProperty, null, link,
                new object[] { "Copy the ILTC weekly schedule into Infloww" });
            linkType.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
        }
        catch
        {
            // A missing shortcut is not worth failing the install over.
        }
    }
}
