// Service worker do Pro Champions: só recebe push e mostra a notificação.
// Não faz cache de página nenhuma — o site continua funcionando igual sem ele.

self.addEventListener("push", function (evento) {
    var dados = {};
    try {
        dados = evento.data ? evento.data.json() : {};
    } catch (e) {
        dados = { titulo: "Pro Champions", corpo: evento.data ? evento.data.text() : "" };
    }
    var titulo = dados.titulo || "Pro Champions";
    var opcoes = {
        body: dados.corpo || "",
        icon: "/static/favicon-192.png",
        badge: "/static/favicon-192.png",
        data: { url: dados.url || "/" },
    };
    evento.waitUntil(self.registration.showNotification(titulo, opcoes));
});

self.addEventListener("notificationclick", function (evento) {
    evento.notification.close();
    var url = (evento.notification.data && evento.notification.data.url) || "/";
    // Se já tem uma aba do site aberta, foca nela e navega; senão abre uma nova.
    evento.waitUntil(
        clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (abas) {
            for (var i = 0; i < abas.length; i++) {
                if ("navigate" in abas[i]) {
                    return abas[i].navigate(url).then(function (aba) { return aba && aba.focus(); });
                }
            }
            return clients.openWindow(url);
        })
    );
});
