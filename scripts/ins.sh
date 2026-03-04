# --- Configuration de ton projet ---
PROJECT_NAME="Mural
VERSION="1.1.2-2"
MAINTAINER_NAME="GaoR"
MAINTAINER_EMAIL="gregoryfaux@etik.com"
DATE_DEB=$(date -R)

# 1. Création de l'arborescence
mkdir -p debian/source

# 2. Création du fichier debian/changelog (Le plus important)
cat <<EOF > debian/changelog
$PROJECT_NAME ($VERSION-1) unstable; urgency=low

  * Initial release.

 -- $MAINTAINER_NAME <$MAINTAINER_EMAIL>  $DATE_DEB
EOF

# 3. Création du fichier debian/control (Les métadonnées)
cat <<EOF > debian/control
Source: $PROJECT_NAME
Section: misc
Priority: optional
Maintainer: $MAINTAINER_NAME <$MAINTAINER_EMAIL>
Build-Depends: debhelper-compat (= 13)
Standards-Version: 4.6.2

Package: $PROJECT_NAME
Architecture: any
Depends: \${shlibs:Depends}, \${misc:Depends}
Description: Description courte de mon projet
 Description longue et détaillée ici.
EOF

# 4. Création du fichier debian/rules (Les instructions de build)
cat <<EOF > debian/rules
#!/usr/bin/make -f
%:
	dh \$@
EOF
chmod +x debian/rules

# 5. Création du format source et du compat
echo "3.0 (quilt)" > debian/source/format
echo "13" > debian/compat

echo "✅ Structure Debian créée avec succès dans $(pwd)/debian"
ls -R debian
