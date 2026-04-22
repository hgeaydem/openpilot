using System;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.IO;
using GTA;
using GTA.Math;

/// <summary>
/// ScriptHookVDotNet script that sends vehicle telemetry over UDP.
///
/// Install:
///   1. Install ScriptHookV (http://www.dev-c.com/gtav/scripthookv/)
///   2. Install ScriptHookVDotNet (https://github.com/scripthookvdotnet/scripthookvdotnet)
///   3. Copy GTAVTelemetry.dll to GTA5/scripts/
///   4. Copy GTAVTelemetry.ini to GTA5/scripts/
///   5. Edit GTAVTelemetry.ini with the bridge machine's IP
///
/// Compile:
///   Reference ScriptHookVDotNet3.dll and System.Net assemblies.
///   Target .NET Framework 4.8.
/// </summary>
public class GTAVTelemetry : Script
{
    private UdpClient udpClient;
    private IPEndPoint endpoint;
    private string targetHost;
    private int targetPort;

    [StructLayout(LayoutKind.Sequential, Pack = 1)]
    struct TelemetryPacket
    {
        public float speed_ms;
        public float heading;
        public float pos_x, pos_y, pos_z;
        public float steering;
        public float throttle;
        public float brake;
        public int gear;
        public float rpm;
        public float vel_x, vel_y, vel_z;
        public float acc_x, acc_y, acc_z;
    }

    public GTAVTelemetry()
    {
        LoadConfig();
        udpClient = new UdpClient();
        endpoint = new IPEndPoint(IPAddress.Parse(targetHost), targetPort);
        Tick += OnTick;
        Interval = 10; // ~100 Hz
    }

    private void LoadConfig()
    {
        targetHost = "192.168.1.50";
        targetPort = 5557;

        string iniPath = Path.Combine(
            Path.GetDirectoryName(typeof(GTAVTelemetry).Assembly.Location),
            "GTAVTelemetry.ini");

        if (File.Exists(iniPath))
        {
            foreach (string line in File.ReadAllLines(iniPath))
            {
                string trimmed = line.Trim();
                if (trimmed.StartsWith(";") || trimmed.StartsWith("#") || !trimmed.Contains("="))
                    continue;

                string[] parts = trimmed.Split(new[] { '=' }, 2);
                string key = parts[0].Trim().ToLower();
                string val = parts[1].Trim();

                if (key == "host") targetHost = val;
                else if (key == "port") int.TryParse(val, out targetPort);
            }
        }
    }

    private void OnTick(object sender, EventArgs e)
    {
        Ped player = Game.Player.Character;
        if (player == null || !player.IsInVehicle())
            return;

        Vehicle vehicle = player.CurrentVehicle;
        if (vehicle == null)
            return;

        Vector3 vel = vehicle.Velocity;
        Vector3 pos = vehicle.Position;

        // Estimate acceleration from velocity change (simple finite difference)
        // GTA doesn't expose raw accelerometer, so we approximate
        float accelX = vel.X * 0.1f; // rough lateral g estimate
        float accelY = 9.81f;        // gravity
        float accelZ = vel.Y * 0.1f; // rough longitudinal g estimate

        TelemetryPacket packet = new TelemetryPacket
        {
            speed_ms = vehicle.Speed,
            heading = vehicle.Heading,
            pos_x = pos.X,
            pos_y = pos.Y,
            pos_z = pos.Z,
            steering = vehicle.SteeringAngle / 40.0f, // normalize to approx [-1, 1]
            throttle = vehicle.Acceleration > 0 ? vehicle.Acceleration : 0,
            brake = vehicle.Acceleration < 0 ? -vehicle.Acceleration : 0,
            gear = vehicle.CurrentGear,
            rpm = vehicle.CurrentRPM,
            vel_x = vel.X,
            vel_y = vel.Y,
            vel_z = vel.Z,
            acc_x = accelX,
            acc_y = accelY,
            acc_z = accelZ,
        };

        byte[] data = StructToBytes(packet);

        try
        {
            udpClient.Send(data, data.Length, endpoint);
        }
        catch (Exception)
        {
            // Silently ignore send failures
        }
    }

    private static byte[] StructToBytes<T>(T str) where T : struct
    {
        int size = Marshal.SizeOf(str);
        byte[] arr = new byte[size];
        IntPtr ptr = Marshal.AllocHGlobal(size);
        Marshal.StructureToPtr(str, ptr, true);
        Marshal.Copy(ptr, arr, 0, size);
        Marshal.FreeHGlobal(ptr);
        return arr;
    }
}
