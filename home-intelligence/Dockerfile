ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN pip3 install --no-cache-dir --break-system-packages flask requests
COPY server.py /app/server.py
COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh
WORKDIR /app
ENTRYPOINT []
CMD ["/app/run.sh"]
