#!/usr/bin/env python3

import os
import sys
import tempfile
import subprocess
import glob

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB
USB_VENDOR_ID = "0471"
USB_PRODUCT_ID = "0055"

# -----------------------
# Helper functions
# -----------------------

def fail(msg):
    print("Content-Type: text/plain\n")
    print("Error:", msg)
    sys.exit(1)

def find_usb_printer_device(vendor_id, product_id):
    """
    Find /dev/usb/lpX by matching USB VID:PID via sysfs.
    Compatible with modern sysfs layout.
    """
    search_paths = glob.glob("/sys/class/usb/lp*") + glob.glob("/sys/class/usbmisc/lp*")

    for sys_path in search_paths:
        try:
            # go one level up to USB device node
            device_dir = os.path.join(sys_path, "device", "..")
            vendor_file = os.path.join(device_dir, "idVendor")
            product_file = os.path.join(device_dir, "idProduct")
            if not (os.path.exists(vendor_file) and os.path.exists(product_file)):
                continue

            with open(vendor_file) as vf:
                vid = vf.read().strip().lower()
            with open(product_file) as pf:
                pid = pf.read().strip().lower()

            if vid == vendor_id.lower() and pid == product_id.lower():
                dev_name = os.path.basename(sys_path)  # lp0, lp1, etc.
                return f"/dev/usb/{dev_name}"

        except Exception:
            continue

    return None

def read_multipart_form():
    """
    Reads multipart/form-data from stdin.
    Returns:
        png_data: bytes or None
        text_data: str or None
        cut_paper: bool
        png_options: dict of PNG-only options
    """
    content_type = os.environ.get("CONTENT_TYPE", "")
    content_length = int(os.environ.get("CONTENT_LENGTH", "0"))

    if "multipart/form-data" not in content_type:
        fail("Form must be multipart/form-data")

    if content_length > MAX_FILE_SIZE + 1024 * 10:
        fail("Upload too large")

    boundary = content_type.split("boundary=")[-1].encode()
    body = sys.stdin.buffer.read(content_length)

    png_data = None
    text_data = None
    cut_paper = False
    png_options = {
        "align": "L",      # default left
        "rotate": False,
        "threshold": 128,  # default
        "photo": False
    }

    parts = body.split(b"--" + boundary)
    for part in parts:
        if not part or part == b"--\r\n":
            continue

        # PNG file
        if b'Content-Disposition' in part and b'name="receipt_image"' in part:
            if b'filename="' in part:
                header, file_data = part.split(b"\r\n\r\n", 1)
                file_data = file_data.rsplit(b"\r\n", 1)[0]
                if len(file_data) > MAX_FILE_SIZE:
                    fail("PNG file too large")
                if file_data.strip():
                    png_data = file_data

        # Text input
        elif b'Content-Disposition' in part and b'name="receipt_text"' in part:
            header, file_data = part.split(b"\r\n\r\n", 1)
            text_data = file_data.rsplit(b"\r\n", 1)[0].decode(errors="ignore").strip()

        # Cut paper checkbox
        elif b'Content-Disposition' in part and b'name="cut_paper"' in part:
            cut_paper = True

        # PNG-only options
        elif b'Content-Disposition' in part:
            if b'name="align"' in part:
                header, val = part.split(b"\r\n\r\n", 1)
                png_options["align"] = val.rsplit(b"\r\n",1)[0].decode().strip().upper()
            elif b'name="rotate"' in part:
                png_options["rotate"] = True
            elif b'name="threshold"' in part:
                header, val = part.split(b"\r\n\r\n",1)
                try:
                    png_options["threshold"] = int(val.rsplit(b"\r\n",1)[0])
                except ValueError:
                    png_options["threshold"] = 128
            elif b'name="photo"' in part:
                png_options["photo"] = True

    if not png_data and not text_data:
        fail("No PNG or text provided")

    return png_data, text_data, cut_paper, png_options

# -----------------------
# Main program
# -----------------------

def main():
    png_data, text_data, cut_paper, png_options = read_multipart_form()

    printer_device = find_usb_printer_device(USB_VENDOR_ID, USB_PRODUCT_ID)
    if not printer_device:
        fail(f"Printer {USB_VENDOR_ID}:{USB_PRODUCT_ID} not found")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:

            if png_data:
                # Save PNG
                png_path = os.path.join(tmpdir, "upload.png")
                with open(png_path, "wb") as f:
                    f.write(png_data)

                # Build png2pos command
                cmd = ["/usr/local/bin/png2pos"]  # adjust path as needed

                if cut_paper:
                    cmd.append("-c")
                if png_options.get("align"):
                    cmd.extend(["-a", png_options["align"]])
                if png_options.get("rotate"):
                    cmd.append("-r")
                if png_options.get("threshold") is not None:
                    cmd.extend(["-t", str(png_options["threshold"])])
                if png_options.get("photo"):
                    cmd.append("-p")

                cmd.append(png_path)

                # Run png2pos
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=True
                )

                # Send to printer
                subprocess.run(
                    ["tee", printer_device],
                    input=result.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=True
                )

            elif text_data:
                # Ensure newline at the end
                if not text_data.endswith("\n"):
                    text_data += "\n"
            
                content = text_data.encode("utf-8")
            
                with open(printer_device, "wb") as printer:
                    # Write the text first
                    printer.write(content)
            
                    # Feed a few blank lines before cutting
                    printer.write(b"\x1B\x64\x05")  # ESC d 5 → feed 5 lines (adjust for gap)
                    printer.flush()
            
                    # Now send the cut command
                    if cut_paper:
                        printer.write(b"\x1D\x56\x00")  # GS V 0 → full cut
                        printer.flush()

    except subprocess.CalledProcessError as e:
        fail(e.stderr.decode(errors="ignore"))
    except subprocess.TimeoutExpired:
        fail("Printer timeout")
    except Exception as e:
        fail(str(e))

    print("Content-Type: text/plain\n")
    print(f"Printed successfully to {printer_device}")

if __name__ == "__main__":
    main()
