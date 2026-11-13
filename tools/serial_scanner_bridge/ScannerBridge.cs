// .NET Framework 4.x; no Python, browser extensions or third-party runtime required.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Ports;
using System.Linq;
using System.Management;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using System.Xml.Serialization;

namespace LvTongScanner {
    public class Settings {
        public string ServerUrl = "http://192.168.88.202:5173";
        public string Port = "COM7";
        public string DeviceId = "USB\\VID_1EAB&PID_1D06\\FFGE2260";
        public int BaudRate = 115200;
        public string EncodingName = "auto";
        public int FrameIdleMs = 400;
        public int DuplicateMs = 3000;
        public int MaxAgeSeconds = 20;
        public bool RtsEnable = true;
        public bool DtrEnable = false;
        public void Validate() {
            Uri uri;
            if (!Uri.TryCreate(ServerUrl, UriKind.Absolute, out uri) ||
                (uri.Scheme != "http" && uri.Scheme != "https") ||
                !String.IsNullOrEmpty(uri.UserInfo + uri.Query + uri.Fragment))
                throw new ArgumentException("目标地址应为 http(s)://IP或域名:端口，不包含账号、查询参数。");
            ServerUrl = ServerUrl.TrimEnd('/');
            if (!Regex.IsMatch(Port ?? "", "^COM[1-9][0-9]*$", RegexOptions.IgnoreCase))
                throw new ArgumentException("请选择有效串口，例如 COM7。");
            if (BaudRate < 1200 || BaudRate > 921600) throw new ArgumentException("波特率超出范围。");
            if (!new [] { "auto", "utf-8", "gb18030" }.Contains(EncodingName)) throw new ArgumentException("字符编码配置无效。");
            if (FrameIdleMs < 100 || FrameIdleMs > 3000 || DuplicateMs < 0 || DuplicateMs > 10000 ||
                MaxAgeSeconds < 5 || MaxAgeSeconds > 60) throw new ArgumentException("分包或重试时间配置无效。");
        }
        public static Settings Load(string path) {
            if (!File.Exists(path)) return new Settings();
            using (var stream = File.OpenRead(path)) return (Settings)new XmlSerializer(typeof(Settings)).Deserialize(stream);
        }
        public void Save(string path) {
            Validate(); Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)));
            using (var stream = File.Create(path)) new XmlSerializer(typeof(Settings)).Serialize(stream, this);
        }
    }

    public static class CodeParser {
        const string Provinces = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼港澳";
        public static string Normalize(string text) {
            text = text.Trim('\uFEFF', '\u0002', '\u0003', '\0', '\r', '\n', '\t', ' ')
                .Replace("｜", "|").Replace("：", ":");
            return text.StartsWith("徑") ? "辽" + text.Substring(1) : text;
        }
        public static bool Valid(string text) {
            var p = text.Split('|');
            if (p.Length < 14 || p[0].Length < 2 || Provinces.IndexOf(p[0][0]) < 0 ||
                Regex.IsMatch(p[0], "[\\u2500-\\u257f\\uFFFD]")) return false;
            foreach (int i in new [] { 3, 4, 5, 9, 10, 11, 13 })
                if (!Regex.IsMatch(p[i].Trim(), "^[0-9]*$")) return false;
            return Regex.IsMatch(p[12].Trim(), "^(?:(?:0[xX])?[0-9a-fA-F]+)?$");
        }
        public static string Decode(byte[] bytes, string encoding) {
            foreach (string name in encoding == "auto" ? new [] { "utf-8", "gb18030" } : new [] { encoding }) {
                try {
                    var codec = Encoding.GetEncoding(name, EncoderFallback.ExceptionFallback, DecoderFallback.ExceptionFallback);
                    string text = Normalize(codec.GetString(bytes));
                    if (Valid(text)) return text;
                } catch (DecoderFallbackException) { }
            }
            return null;
        }
        public static string Plate(string raw) { return Regex.Replace(raw.Split('|')[0], "_[0-9]+$", ""); }
    }

    public sealed class Framer {
        readonly List<byte> bytes = new List<byte>();
        readonly Action<byte[]> complete;
        readonly Action invalid;
        readonly int idleMs;
        DateTime lastByte;
        bool overflow;
        public Framer(int idleMs, Action<byte[]> complete, Action invalid) {
            this.idleMs = idleMs; this.complete = complete; this.invalid = invalid;
        }
        public void Feed(byte[] data, int count, DateTime now) {
            for (int i = 0; i < count; i++) {
                byte b = data[i];
                if (b == 2) { bytes.Clear(); overflow = false; continue; }
                if (b == 3 || b == 10 || b == 13) { Finish(); continue; }
                if (b == 0) continue;
                if (!overflow) {
                    bytes.Add(b);
                    if (bytes.Count > 8192) { bytes.Clear(); overflow = true; invalid(); }
                }
                lastByte = now;
            }
        }
        public void Tick(DateTime now) { if ((bytes.Count > 0 || overflow) && (now - lastByte).TotalMilliseconds >= idleMs) Finish(); }
        void Finish() {
            if (bytes.Count > 0 && !overflow) complete(bytes.ToArray());
            bytes.Clear(); overflow = false;
        }
    }

    public sealed class Packet {
        public string Raw;
        public DateTime At;
        public string Name;
        public Packet(string raw, DateTime at) {
            Raw = raw; At = at;
            Name = "photo_serial_" + at.ToString("yyyyMMddHHmmssfff") + "_" + Guid.NewGuid().ToString("N") + ".json";
        }
    }

    public static class Upload {
        public static void Send(Settings config, Packet packet, Action<HttpWebRequest> current) {
            string boundary = "----LvTongScanner" + Guid.NewGuid().ToString("N");
            var document = new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                { "passcodeQRCodeInfo", packet.Raw }
            });
            byte[] body = Encoding.UTF8.GetBytes("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\"" +
                packet.Name + "\"\r\nContent-Type: application/json; charset=utf-8\r\n\r\n" + document + "\r\n--" + boundary + "--\r\n");
            var req = (HttpWebRequest)WebRequest.Create(config.ServerUrl.TrimEnd('/') + "/api/mobile/upload/json");
            req.Method = "POST"; req.Proxy = null; req.Timeout = 5000; req.ReadWriteTimeout = 5000;
            req.AllowAutoRedirect = false; req.KeepAlive = false;
            req.ContentType = "multipart/form-data; boundary=" + boundary; req.ContentLength = body.Length;
            current(req);
            try {
                using (var stream = req.GetRequestStream()) stream.Write(body, 0, body.Length);
                using (var res = (HttpWebResponse)req.GetResponse())
                using (var stream = new StreamReader(res.GetResponseStream(), Encoding.UTF8)) {
                    var json = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(stream.ReadToEnd());
                    if (res.StatusCode != HttpStatusCode.OK || !json.ContainsKey("code") || Convert.ToInt32(json["code"]) != 0)
                        throw new IOException("服务端未确认接收扫码结果。");
                }
            } finally { current(null); }
        }
    }

    public sealed class Bridge : IDisposable {
        readonly Settings config;
        readonly string directory;
        readonly ManualResetEvent stop = new ManualResetEvent(false);
        readonly BlockingCollection<Packet> queue = new BlockingCollection<Packet>(16);
        readonly object gate = new object();
        readonly Dictionary<string, object> state = new Dictionary<string, object>();
        Thread reader, sender;
        volatile HttpWebRequest currentRequest;
        string previousCode;
        DateTime previousAt;
        int received, uploaded;
        public Bridge(Settings config, string directory) { config.Validate(); this.config = config; this.directory = directory; }
        public void Start() {
            Directory.CreateDirectory(directory);
            Status("server", config.ServerUrl); Status("port", config.Port); Status("portState", "正在连接串口");
            Status("uploadState", "等待扫码");
            reader = new Thread(ReadLoop) { IsBackground = true, Name = "scanner-serial" };
            sender = new Thread(UploadLoop) { IsBackground = true, Name = "scanner-upload" };
            reader.Start(); sender.Start();
        }
        public void Stop() {
            stop.Set(); var req = currentRequest; if (req != null) req.Abort();
            if (reader != null) reader.Join(2500); if (sender != null) sender.Join(5500);
            Status("portState", "服务已停止"); Log("服务已停止。");
        }
        public void Dispose() { Stop(); }
        void Status(string key, object value) {
            lock (gate) {
                state[key] = value; state["updatedAt"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
                try {
                    string path = Path.Combine(directory, "status.json"), tmp = path + ".tmp";
                    File.WriteAllText(tmp, new JavaScriptSerializer().Serialize(state), Encoding.UTF8);
                    if (File.Exists(path)) File.Replace(tmp, path, null); else File.Move(tmp, path);
                } catch (IOException) { } catch (UnauthorizedAccessException) { }
            }
        }
        void Log(string message) {
            lock (gate) {
                try {
                    string path = Path.Combine(directory, "scanner.log");
                    if (File.Exists(path) && new FileInfo(path).Length > 2 * 1024 * 1024) {
                        File.Delete(path + ".1"); File.Move(path, path + ".1");
                    }
                    File.AppendAllText(path, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine, Encoding.UTF8);
                } catch (IOException) { } catch (UnauthorizedAccessException) { }
            }
        }
        string FindPort() {
            if (String.IsNullOrWhiteSpace(config.DeviceId)) return config.Port;
            try {
                using (var searcher = new ManagementObjectSearcher("SELECT DeviceID, PNPDeviceID FROM Win32_SerialPort"))
                using (var found = searcher.Get()) {
                    foreach (ManagementObject p in found)
                        if (String.Equals(Convert.ToString(p["PNPDeviceID"]), config.DeviceId, StringComparison.OrdinalIgnoreCase))
                            return Convert.ToString(p["DeviceID"]);
                }
            } catch (ManagementException) { }
            // 指定设备离线时不能打开另一个恰好复用了 COM 号的设备。
            throw new IOException("扫码枪未连接：" + config.Port);
        }
        void ReadLoop() {
            while (!stop.WaitOne(0)) {
                try {
                    string name = FindPort();
                    using (var port = new SerialPort(name, config.BaudRate, Parity.None, 8, StopBits.One)) {
                        port.ReadTimeout = 100; port.Handshake = Handshake.None;
                        port.DtrEnable = config.DtrEnable; port.RtsEnable = config.RtsEnable;
                        port.Open(); Status("port", name); Status("portState", "已连接，等待扫码");
                        Log("串口已连接 " + name + " " + config.BaudRate + " 8N1。");
                        var framer = new Framer(config.FrameIdleMs, OnFrame, () => Invalid("扫码数据过长，已丢弃，请重新扫描。"));
                        byte[] bytes = new byte[4096];
                        while (!stop.WaitOne(0)) {
                            try { int n = port.Read(bytes, 0, bytes.Length); framer.Feed(bytes, n, DateTime.UtcNow); }
                            catch (System.TimeoutException) { framer.Tick(DateTime.UtcNow); }
                        }
                    }
                } catch (Exception e) {
                    if (stop.WaitOne(0)) break;
                    Status("portState", "连接异常，正在自动重连"); Status("error", e.Message); Log("串口异常：" + e.Message);
                    stop.WaitOne(2000);
                }
            }
        }
        void Invalid(string error) { Status("error", error); Log(error); }
        void OnFrame(byte[] bytes) {
            var raw = CodeParser.Decode(bytes, config.EncodingName);
            if (raw == null) { Invalid("收到 " + bytes.Length + " 字节，但不是完整有效的通行码，请检查编码或重新扫描。"); return; }
            var now = DateTime.UtcNow;
            if (previousCode == raw && (now - previousAt).TotalMilliseconds < config.DuplicateMs) return;
            previousCode = raw; previousAt = now;
            var packet = new Packet(raw, now);
            Status("lastPlate", CodeParser.Plate(raw)); Status("lastScanAt", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            Status("received", ++received); Status("error", "");
            if (!queue.TryAdd(packet)) { Invalid("上传队列已满，本次未发送，请稍后重新扫描。"); return; }
            Log("收到通行码 " + CodeParser.Plate(raw) + "，已排队发送。");
        }
        void UploadLoop() {
            while (!stop.WaitOne(0)) {
                Packet packet;
                if (!queue.TryTake(out packet, 200)) continue;
                bool ok = false;
                for (int attempt = 0; attempt < 3 && !stop.WaitOne(0); attempt++) {
                    if ((DateTime.UtcNow - packet.At).TotalSeconds > config.MaxAgeSeconds) break;
                    try {
                        Status("uploadState", "正在发送 " + CodeParser.Plate(packet.Raw));
                        Upload.Send(config, packet, req => currentRequest = req);
                        Status("uploaded", ++uploaded); Status("lastUploadAt", DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
                        Status("uploadState", "目标服务已接收，等待下一次扫码"); Status("error", "");
                        Log("上传成功 " + CodeParser.Plate(packet.Raw) + " -> " + config.ServerUrl + "，文件=" + packet.Name);
                        ok = true; break;
                    } catch (Exception e) {
                        if (stop.WaitOne(0)) break;
                        Status("error", "上传失败：" + e.Message); Log("上传失败（第" + (attempt + 1) + "次）：" + e.Message);
                        if (stop.WaitOne(1000 * (attempt + 1))) break;
                    }
                }
                if (!ok && !stop.WaitOne(0)) {
                    Status("uploadState", "本次未送达，请重新扫描");
                    Log("本次重试结束，不留存旧码继续补发，请重新扫描。");
                }
            }
        }
    }

    public sealed class ScannerService : ServiceBase {
        Bridge bridge;
        public ScannerService() { ServiceName = Program.ServiceName; CanStop = true; AutoLog = true; }
        protected override void OnStart(string[] args) {
            bridge = new Bridge(Settings.Load(Program.ConfigPath), Program.DataDirectory); bridge.Start();
        }
        protected override void OnStop() { if (bridge != null) { bridge.Stop(); bridge = null; } }
    }

    public static class Program {
        public const string ServiceName = "LvTongScannerBridge";
        public static readonly string DataDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), ServiceName);
        public static readonly string ConfigPath = Path.Combine(DataDirectory, "scanner-config.xml");
        public static readonly string InstalledExe = Path.Combine(DataDirectory, "ScannerBridge.exe");
        public static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
        static bool IsAdmin() { return new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator); }
        public static bool Exists() { return ServiceController.GetServices().Any(s => s.ServiceName == ServiceName); }
        public static void StopService() {
            if (!Exists()) return;
            using (var svc = new ServiceController(ServiceName)) {
                if (svc.Status != ServiceControllerStatus.Stopped) { svc.Stop(); svc.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(20)); }
            }
        }
        static void StartService() {
            using (var svc = new ServiceController(ServiceName)) {
                if (svc.Status == ServiceControllerStatus.StopPending) svc.WaitForStatus(ServiceControllerStatus.Stopped, TimeSpan.FromSeconds(20));
                if (svc.Status == ServiceControllerStatus.Stopped) svc.Start();
                svc.WaitForStatus(ServiceControllerStatus.Running, TimeSpan.FromSeconds(20));
            }
        }
        static void Sc(string args) {
            var start = new ProcessStartInfo("sc.exe", args) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            using (var p = Process.Start(start)) {
                string output = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd(); p.WaitForExit();
                if (p.ExitCode != 0) throw new IOException("服务管理失败（" + p.ExitCode + "）：" + output);
            }
        }
        static void Shortcut(bool create) {
            string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonDesktopDirectory), "绿通扫码服务管理.lnk");
            if (!create) { if (File.Exists(path)) File.Delete(path); return; }
            var shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            var shortcut = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
            shortcut.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { InstalledExe });
            shortcut.GetType().InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "绿通串口扫码服务配置、重启和卸载" });
            shortcut.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
        }
        public static void AdminAction(string action, string configFile) {
            if (!IsAdmin()) {
                string args = action + " --quiet" + (String.IsNullOrEmpty(configFile) ? "" : " --config " + Quote(configFile));
                using (var p = Process.Start(new ProcessStartInfo(Application.ExecutablePath, args) { UseShellExecute = true, Verb = "runas" })) {
                    p.WaitForExit(); if (p.ExitCode != 0) throw new IOException("操作未完成，请查看 Windows 提示或日志。");
                }
                return;
            }
            if (action == "--install" || action == "--configure") {
                if (action == "--configure" && !Exists())
                    throw new InvalidOperationException("服务尚未安装，请直接点击“安装并开机启动”，将使用当前填写的配置安装。");
                Settings config = Settings.Load(configFile); config.Validate(); StopService();
                Directory.CreateDirectory(DataDirectory);
                if (action == "--install" && !String.Equals(Application.ExecutablePath, InstalledExe, StringComparison.OrdinalIgnoreCase))
                    File.Copy(Application.ExecutablePath, InstalledExe, true);
                config.Save(ConfigPath);
                if (!Exists()) Sc("create " + ServiceName + " binPath= " + Quote(Quote(InstalledExe) + " --service") + " start= auto DisplayName= " + Quote("绿通串口扫码服务"));
                else Sc("config " + ServiceName + " start= auto");
                Sc("description " + ServiceName + " " + Quote("读取检测电脑串口扫码枪，将通行码发送至配置的绿通服务地址并同步页面。"));
                Sc("failure " + ServiceName + " reset= 86400 actions= restart/5000/restart/15000/restart/60000");
                StartService(); Shortcut(true);
            } else if (action == "--start") StartService();
            else if (action == "--stop") StopService();
            else if (action == "--restart") { StopService(); StartService(); }
            else if (action == "--uninstall") {
                StopService(); if (Exists()) Sc("delete " + ServiceName); Shortcut(false);
                // 配置和有限日志保留，方便重装；卸载会取消自动启动且不再占用串口。
            } else throw new ArgumentException("未知服务操作。");
        }
        [STAThread]
        public static int Main(string[] args) {
            try {
                if (args.Contains("--self-test")) {
                    int sample = Array.IndexOf(args, "--sample");
                    SelfTest.Run(sample >= 0 && sample + 1 < args.Length ? args[sample + 1] : null); return 0;
                }
                if (args.Contains("--service") || !Environment.UserInteractive) { ServiceBase.Run(new ScannerService()); return 0; }
                if (args.Length > 0) {
                    int index = Array.IndexOf(args, "--config");
                    string config = index >= 0 && index + 1 < args.Length ? args[index + 1] :
                        (File.Exists(ConfigPath) ? ConfigPath : Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "scanner-config.xml"));
                    AdminAction(args[0], config);
                    string oldError = Path.Combine(Path.GetTempPath(), "lvtong-scanner-operation-error.txt");
                    try { File.Delete(oldError); } catch (IOException) { } catch (UnauthorizedAccessException) { }
                    if (!args.Contains("--quiet")) MessageBox.Show("操作已完成。可双击“绿通扫码服务管理”查看状态及修改地址。", "绿通扫码服务", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return 0;
                }
                Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new ManagerForm()); return 0;
            } catch (Exception e) {
                if (!Environment.UserInteractive) throw;
                if (!args.Contains("--quiet")) MessageBox.Show(e.Message, "绿通扫码服务", MessageBoxButtons.OK, MessageBoxIcon.Error);
                else File.WriteAllText(Path.Combine(Path.GetTempPath(), "lvtong-scanner-operation-error.txt"), e.ToString());
                return 1;
            }
        }
    }

    public sealed class ManagerForm : Form {
        readonly TextBox server = new TextBox();
        readonly ComboBox ports = new ComboBox(), baud = new ComboBox(), encoding = new ComboBox();
        readonly TextBox status = new TextBox();
        readonly Label serviceState = new Label();
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        Settings settings;
        bool working;
        public ManagerForm() {
            Text = "绿通串口扫码服务管理"; ClientSize = new Size(660, 525); Font = new Font("Microsoft YaHei UI", 10);
            FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
            settings = Settings.Load(File.Exists(Program.ConfigPath) ? Program.ConfigPath : Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "scanner-config.xml"));
            LabelAt("目标服务地址", 20, 22); server.SetBounds(140, 18, 495, 30); server.Text = settings.ServerUrl; Controls.Add(server);
            LabelAt("扫码枪串口", 20, 67); ports.SetBounds(140, 63, 130, 30); ports.Items.AddRange(SerialPort.GetPortNames()); ports.Text = settings.Port; Controls.Add(ports);
            LabelAt("波特率", 300, 67); baud.SetBounds(380, 63, 120, 30); baud.Items.AddRange(new object[] { "9600", "19200", "38400", "57600", "115200" }); baud.Text = settings.BaudRate.ToString(); Controls.Add(baud);
            ButtonAt("刷新串口", 520, 62, 115, () => { string old = ports.Text; ports.Items.Clear(); ports.Items.AddRange(SerialPort.GetPortNames()); ports.Text = old; });
            LabelAt("字符编码", 20, 112); encoding.SetBounds(140, 108, 130, 30); encoding.DropDownStyle = ComboBoxStyle.DropDownList;
            encoding.Items.AddRange(new object[] { "自动识别", "UTF-8", "GB18030/GBK" }); encoding.SelectedIndex = settings.EncodingName == "auto" ? 0 : settings.EncodingName == "utf-8" ? 1 : 2; Controls.Add(encoding);
            LabelAt("数据格式：8 数据位、无校验、1 停止位", 295, 112);
            ButtonAt("测试目标连接", 20, 155, 145, async () => {
                try {
                    var c = ReadSettings();
                    await Task.Run(() => {
                        var req = (HttpWebRequest)WebRequest.Create(c.ServerUrl + "/api/mobile/health"); req.Proxy = null; req.Timeout = 5000;
                        using (var res = req.GetResponse()) using (var reader = new StreamReader(res.GetResponseStream())) {
                            var body = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
                            if (!body.ContainsKey("code") || Convert.ToInt32(body["code"]) != 0) throw new IOException("未收到有效绿通服务响应。");
                        }
                    }); MessageBox.Show("目标服务连接正常。", Text);
                } catch (Exception e) { MessageBox.Show(e.Message, "连接失败"); }
            });
            ButtonAt("保存配置并重启", 180, 155, 180, () => Action("--configure", true));
            ButtonAt("安装并开机启动", 375, 155, 260, () => Action("--install", true));
            ButtonAt("启动服务", 20, 205, 140, () => Action("--start", false));
            ButtonAt("停止服务", 180, 205, 140, () => Action("--stop", false));
            ButtonAt("重启服务", 340, 205, 140, () => Action("--restart", false));
            ButtonAt("卸载服务", 500, 205, 135, () => { if (MessageBox.Show("卸载后不再开机运行，也不再读取扫码枪。是否继续？", Text, MessageBoxButtons.YesNo) == DialogResult.Yes) Action("--uninstall", false); });
            serviceState.SetBounds(20, 258, 615, 26); Controls.Add(serviceState);
            status.SetBounds(20, 290, 615, 165); status.Multiline = true; status.ReadOnly = true; status.ScrollBars = ScrollBars.Vertical; Controls.Add(status);
            ButtonAt("打开日志目录", 20, 475, 150, () => { Directory.CreateDirectory(Program.DataDirectory); Process.Start("explorer.exe", Program.Quote(Program.DataDirectory)); });
            LabelAt("修改 IP、端口或串口后，点击“保存配置并重启”。", 190, 482);
            timer.Interval = 1000; timer.Tick += (s, e) => RefreshStatus(); timer.Start(); RefreshStatus();
            FormClosed += (s, e) => timer.Dispose();
        }
        void LabelAt(string text, int x, int y) { var label = new Label { Text = text, AutoSize = true, Location = new Point(x, y) }; Controls.Add(label); }
        void ButtonAt(string text, int x, int y, int width, Action action) {
            var button = new Button { Text = text }; button.SetBounds(x, y, width, 36); button.Click += (s, e) => { if (!working) action(); }; Controls.Add(button);
        }
        Settings ReadSettings() {
            var c = Settings.Load(File.Exists(Program.ConfigPath) ? Program.ConfigPath : Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "scanner-config.xml"));
            c.ServerUrl = server.Text.Trim(); c.Port = ports.Text.Trim().ToUpperInvariant(); c.BaudRate = Int32.Parse(baud.Text.Trim());
            c.EncodingName = new [] { "auto", "utf-8", "gb18030" }[encoding.SelectedIndex];
            c.DeviceId = "";
            using (var searcher = new ManagementObjectSearcher("SELECT DeviceID, PNPDeviceID FROM Win32_SerialPort"))
            using (var found = searcher.Get()) foreach (ManagementObject p in found)
                if (String.Equals(Convert.ToString(p["DeviceID"]), c.Port, StringComparison.OrdinalIgnoreCase)) c.DeviceId = Convert.ToString(p["PNPDeviceID"]);
            c.Validate(); return c;
        }
        async void Action(string action, bool withConfig) {
            string tmp = null; working = true;
            try {
                if (withConfig) { tmp = Path.Combine(Path.GetTempPath(), "lvtong-scanner-" + Guid.NewGuid().ToString("N") + ".xml"); ReadSettings().Save(tmp); }
                await Task.Run(() => Program.AdminAction(action, tmp)); RefreshStatus();
            } catch (Exception e) { MessageBox.Show(e.Message, Text); }
            finally { if (tmp != null && File.Exists(tmp)) File.Delete(tmp); working = false; }
        }
        void RefreshStatus() {
            try {
                if (!Program.Exists()) serviceState.Text = "服务状态：未安装";
                else using (var svc = new ServiceController(Program.ServiceName)) serviceState.Text = "服务状态：" + svc.Status + "（已设置开机自动启动）";
                string file = Path.Combine(Program.DataDirectory, "status.json");
                if (File.Exists(file)) {
                    var d = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(file, Encoding.UTF8));
                    Func<string, string> get = k => d.ContainsKey(k) ? Convert.ToString(d[k]) : "";
                    status.Text = "串口：" + get("port") + "  " + get("portState") + "\r\n目标：" + get("server") +
                        "\r\n上传：" + get("uploadState") + "\r\n最近车牌：" + get("lastPlate") + "   收到/上传：" + get("received") + "/" + get("uploaded") +
                        "\r\n最近扫码：" + get("lastScanAt") + "   最近上传：" + get("lastUploadAt") + "\r\n错误：" + get("error");
                } else status.Text = "安装并启动服务后，此处显示串口连接、扫码和上传状态。";
            } catch (Exception e) { serviceState.Text = "状态读取失败：" + e.Message; }
        }
    }

    public static class SelfTest {
        static void Assert(bool value, string name) { if (!value) throw new Exception("测试失败：" + name); }
        public static void Run(string sampleFile) {
            string raw = "鄂ACB1870_4|S0002420010010|S0017420020020|4120|3140|2|S001742002002040101302026111312139932|020000450101620038065020261113113728|2026-11-13T12:13:43|0|2600|2600|0xFF|1";
            Assert(CodeParser.Decode(Encoding.UTF8.GetBytes(raw), "auto") == raw, "UTF8中文码");
            Assert(CodeParser.Decode(Encoding.GetEncoding("gb18030").GetBytes(raw), "auto") == raw, "GBK中文码");
            Assert(CodeParser.Decode(Encoding.UTF8.GetBytes("40|2|x|y|z"), "auto") == null, "截断码拒绝");
            Assert(!CodeParser.Valid(raw.Replace("|3140|", "|NaN|")), "数值损坏拒绝");
            var frames = new List<byte[]>(); int invalid = 0;
            var framer = new Framer(400, b => frames.Add(b), () => invalid++);
            var now = DateTime.UtcNow; var data = Encoding.UTF8.GetBytes("\x02" + raw + "\r\n" + raw + "\x03");
            for (int i = 0; i < data.Length; i++) framer.Feed(new [] { data[i] }, 1, now);
            Assert(frames.Count == 2 && frames.All(b => CodeParser.Decode(b, "auto") == raw), "中文拆字节及连续两帧");
            frames.Clear(); data = Encoding.UTF8.GetBytes(raw); framer.Feed(data, data.Length, now);
            framer.Tick(now.AddMilliseconds(399)); Assert(frames.Count == 0, "无结束符等待完整包");
            framer.Tick(now.AddMilliseconds(401)); Assert(frames.Count == 1, "无结束符400ms完成");
            frames.Clear(); data = new byte[9000]; for (int i = 0; i < data.Length; i++) data[i] = 65;
            framer.Feed(data, data.Length, now); framer.Feed(new byte[] { 13 }, 1, now);
            Assert(invalid == 1 && frames.Count == 0, "超长包拒绝");
            var cfg = new Settings(); cfg.Validate(); cfg.ServerUrl = "file:///C:/test";
            bool rejected = false; try { cfg.Validate(); } catch (ArgumentException) { rejected = true; }
            Assert(rejected, "目标URL校验");
            if (!String.IsNullOrEmpty(sampleFile)) Assert(CodeParser.Decode(File.ReadAllBytes(sampleFile), "auto") == raw, "真实串口原始字节与用户二维码一致");
            // 真正HTTP multipart往返，仅访问本地随机端口，不连接现场。
            var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
            int port = ((IPEndPoint)listener.LocalEndpoint).Port; Exception serverError = null;
            var server = new Thread(() => {
                try {
                    using (var client = listener.AcceptTcpClient()) using (var stream = client.GetStream()) {
                        stream.ReadTimeout = 5000; var header = new List<byte>();
                        while (true) { int b = stream.ReadByte(); if (b < 0) throw new IOException(); header.Add((byte)b); if (header.Count >= 4 && Encoding.ASCII.GetString(header.ToArray()).EndsWith("\r\n\r\n")) break; }
                        string text = Encoding.ASCII.GetString(header.ToArray());
                        Assert(text.StartsWith("POST /api/mobile/upload/json HTTP/"), "上传接口路径");
                        int length = Int32.Parse(Regex.Match(text, "Content-Length: ([0-9]+)", RegexOptions.IgnoreCase).Groups[1].Value);
                        var body = new byte[length]; int at = 0; while (at < length) { int n = stream.Read(body, at, length-at); if (n == 0) throw new IOException(); at += n; }
                        Assert(Encoding.UTF8.GetString(body).Contains(raw), "multipart完整中文通行码");
                        byte[] reply = Encoding.UTF8.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 10\r\nConnection: close\r\n\r\n{\"code\":0}"); stream.Write(reply, 0, reply.Length);
                    }
                } catch (Exception e) { serverError = e; }
            }); server.IsBackground = true; server.Start();
            try { Upload.Send(new Settings { ServerUrl = "http://127.0.0.1:" + port }, new Packet(raw, now), req => { }); }
            finally { server.Join(7000); listener.Stop(); }
            if (serverError != null) throw serverError;
            Application.EnableVisualStyles();
            using (var form = new ManagerForm()) using (var bitmap = new Bitmap(form.Width, form.Height)) {
                form.StartPosition = FormStartPosition.Manual;
                form.Location = new Point(-2000, -2000); form.ShowInTaskbar = false;
                form.Show(); form.PerformLayout(); Application.DoEvents();
                form.DrawToBitmap(bitmap, new Rectangle(0, 0, form.Width, form.Height));
                bitmap.Save(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "manager-preview.png"));
                form.Close();
            }
            File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "self-test-result.txt"), "PASS: UTF8/GBK, framing, corruption/overflow rejection, target configuration, real local HTTP multipart upload, management UI" + (sampleFile == null ? "." : ", real COM7 sample matches user QR."));
        }
    }
}
