using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

// Own the complete Python process tree, including the Windows venv launcher.
// Windows closes this job handle even when the user closes the console with X.
public sealed class VertexConsoleProcess : IDisposable
{
    private IntPtr job = IntPtr.Zero;
    private IntPtr process = IntPtr.Zero;
    public int ProcessId { get; private set; }
    public bool HasExited { get { return WaitForSingleObject(process, 0) == 0; } }

    public VertexConsoleProcess(string executable, string workingDirectory, string stdout, string stderr)
    {
        IntPtr output = IntPtr.Zero, error = IntPtr.Zero;
        PROCESS_INFORMATION child = new PROCESS_INFORMATION();
        try
        {
            job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) throw new Win32Exception();
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = 0x2000; // KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(job, 9, ref limits, Marshal.SizeOf(limits)))
                throw new Win32Exception();

            SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
            security.Length = Marshal.SizeOf(security);
            security.InheritHandle = 1;
            output = CreateFile(stdout, 0x40000000, 7, ref security, 2, 0x80, IntPtr.Zero);
            error = CreateFile(stderr, 0x40000000, 7, ref security, 2, 0x80, IntPtr.Zero);
            if (output == new IntPtr(-1) || error == new IntPtr(-1)) throw new Win32Exception();
            STARTUPINFO startup = new STARTUPINFO();
            startup.Size = Marshal.SizeOf(startup);
            startup.Flags = 0x100; // STARTF_USESTDHANDLES
            startup.StdOutput = output;
            startup.StdError = error;
            // Start suspended so even children created by the venv launcher belong to our job.
            StringBuilder command = new StringBuilder("\"" + executable + "\" -u -m uvicorn main:app --host 0.0.0.0 --port 8050");
            if (!CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                               0x08000004, IntPtr.Zero, workingDirectory, ref startup, out child))
                throw new Win32Exception();
            process = child.Process;
            ProcessId = child.ProcessId;
            if (!AssignProcessToJobObject(job, process)) throw new Win32Exception();
            if (ResumeThread(child.Thread) == UInt32.MaxValue) throw new Win32Exception();
        }
        catch
        {
            if (process != IntPtr.Zero) TerminateProcess(process, 1);
            Dispose();
            throw;
        }
        finally
        {
            if (child.Thread != IntPtr.Zero) CloseHandle(child.Thread);
            if (output != IntPtr.Zero && output != new IntPtr(-1)) CloseHandle(output);
            if (error != IntPtr.Zero && error != new IntPtr(-1)) CloseHandle(error);
        }
    }

    public void Dispose()
    {
        if (job != IntPtr.Zero) { CloseHandle(job); job = IntPtr.Zero; }
        if (process != IntPtr.Zero) { CloseHandle(process); process = IntPtr.Zero; }
    }

    [StructLayout(LayoutKind.Sequential)] private struct SECURITY_ATTRIBUTES
    { public int Length; public IntPtr Descriptor; public int InheritHandle; }
    [StructLayout(LayoutKind.Sequential)] private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)] private struct IO_COUNTERS
    { public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
    [StructLayout(LayoutKind.Sequential)] private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] private struct STARTUPINFO
    {
        public int Size; public string Reserved, Desktop, Title;
        public int X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute, Flags;
        public short ShowWindow, ReservedSize; public IntPtr ReservedPointer, StdInput, StdOutput, StdError;
    }
    [StructLayout(LayoutKind.Sequential)] private struct PROCESS_INFORMATION
    { public IntPtr Process, Thread; public int ProcessId, ThreadId; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, int length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(string name, uint access, uint share, ref SECURITY_ATTRIBUTES security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string application, StringBuilder command, IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string directory, ref STARTUPINFO startup, out PROCESS_INFORMATION process);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll")] private static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
}
