<p align="center">
  <img src="assets/logo.png" width="850">
</p>

<p align="center">
  <strong>Wallpaper manager for GNOME.</strong><br>
  Per-monitor wallpapers. Slideshow. Native GTK4.
</p>

<hr>

<h2>Install (Ubuntu 24.04+)</h2>

<p>Recommended method.</p>

<pre><code>sudo add-apt-repository ppa:gaor/mural
sudo apt update
sudo apt install mural</code></pre>

<p>Launch <strong>Mural</strong> from GNOME Activities.</p>

<hr>

<h2>Manual install (no PPA)</h2>

<p><strong>Step 1 — Install dependencies</strong></p>

<pre><code>sudo apt install python3-gi python3-pil gir1.2-gtk-4.0 gir1.2-adw-1</code></pre>

<p><strong>Step 2 — Download and install</strong></p>

<pre><code>git clone https://github.com/gaorfg-bit/mural
cd mural
pip install -r requirements.txt
./install.sh</code></pre>

<p><strong>Uninstall:</strong></p>

<pre><code>./uninstall.sh</code></pre>

<hr>

<h2>Features</h2>

<ul>
  <li>Independent wallpapers per monitor</li>
  <li>No overwriting</li>
  <li>Slideshow mode</li>
  <li>Optional AVIF compression</li>
</ul>

<hr>

<p align="center">
  <img src="assets/Mural.png" width="900">
</p>

<hr>

<p>GNOME 46+, Wayland or X11.</p>

<p>GPL-3.0 — © 2026 GaoR</p>
