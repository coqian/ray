// Copyright 2022 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include <grpcpp/grpcpp.h>

#include <cstdint>
#include <functional>
#include <memory>
#include <vector>

#include "absl/container/flat_hash_map.h"
#include "ray/common/asio/instrumented_io_context.h"
#include "ray/common/id.h"
#include "ray/common/ray_config.h"
#include "ray/util/thread_checker.h"
#include "src/proto/grpc/health/v1/health.grpc.pb.h"

namespace ray::gcs {

/// GcsHealthCheckManager is used to track the healthiness of the nodes in the ray
/// cluster. The health check is done in pull based way, which means this module will send
/// health check to the raylets to see whether the raylet is healthy or not. If the raylet
/// is not healthy for certain times, the module will think the raylet is dead.
/// When the node is dead a callback passed in the constructor will be called and this
/// node will be removed from GcsHealthCheckManager. The node can be added into this class
/// later. Although the same node id is not supposed to be reused in ray cluster, this is
/// not enforced in this class.
///
/// All IO operations happens on the same thread, which is managed by the pass-ed in
/// [io_service].
/// TODO (iycheng): Move the GcsHealthCheckManager to ray/common.
class GcsHealthCheckManager : public std::enable_shared_from_this<GcsHealthCheckManager> {
 public:
  /// Factory constructor of GcsHealthCheckManager.
  ///
  /// \param io_service The thread where all operations in this class should run.
  /// \param on_node_death_callback The callback function when some node is marked as
  /// failure.
  /// \param initial_delay_ms The delay for the first health check.
  /// \param period_ms The interval between two health checks for the same node.
  /// \param failure_threshold The threshold before a node will be marked as dead due to
  /// health check failure.
  static std::shared_ptr<GcsHealthCheckManager> Create(
      instrumented_io_context &io_service,
      std::function<void(const NodeID &)> on_node_death_callback,
      int64_t initial_delay_ms = RayConfig::instance().health_check_initial_delay_ms(),
      int64_t timeout_ms = RayConfig::instance().health_check_timeout_ms(),
      int64_t period_ms = RayConfig::instance().health_check_period_ms(),
      int64_t failure_threshold = RayConfig::instance().health_check_failure_threshold());

  ~GcsHealthCheckManager();

  /// Start to track the healthiness of a node.
  /// Safe to call from non-io-context threads.
  ///
  /// \param node_id The id of the node.
  /// \param channel The gRPC channel to the node.
  void AddNode(const NodeID &node_id, std::shared_ptr<grpc::Channel> channel);

  /// Stop tracking the healthiness of a node.
  /// Safe to call from non-io-context threads.
  ///
  /// \param node_id The id of the node to stop tracking.
  void RemoveNode(const NodeID &node_id);

  /// Return all the nodes monitored and alive.
  /// Notice: have to invoke from io-context thread.
  ///
  /// \return A list of node id which are being monitored by this class.
  std::vector<NodeID> GetAllNodes() const;

  /// Mark the given node as healthy, so health check manager could save some checking
  /// rpcs. Safe to call from non-io-context threads.
  ///
  /// \param node_id The id of the node.
  void MarkNodeHealthy(const NodeID &node_id);

 private:
  GcsHealthCheckManager(instrumented_io_context &io_service,
                        std::function<void(const NodeID &)> on_node_death_callback,
                        int64_t initial_delay_ms,
                        int64_t timeout_ms,
                        int64_t period_ms,
                        int64_t failure_threshold);

  /// Fail a node when health check failed. It'll stop the health checking and
  /// call `on_node_death_callback_`.
  ///
  /// \param node_id The id of the node.
  void FailNode(const NodeID &node_id);

  using Timer = boost::asio::deadline_timer;

  /// The context for the health check. It's to support unary call.
  /// It can be updated to support streaming call for efficiency.
  class HealthCheckContext {
   public:
    HealthCheckContext(std::shared_ptr<GcsHealthCheckManager> manager,
                       std::shared_ptr<grpc::Channel> channel,
                       NodeID node_id)
        : manager_(manager),
          node_id_(node_id),
          timer_(manager->io_service_),
          health_check_remaining_(manager->failure_threshold_) {
      request_.set_service(node_id.Hex());
      stub_ = grpc::health::v1::Health::NewStub(std::move(channel));
      timer_.expires_from_now(
          boost::posix_time::milliseconds(manager->initial_delay_ms_));
      timer_.async_wait([this](auto) { StartHealthCheck(); });
    }

    void Stop();

    void SetLatestHealthTimestamp(absl::Time ts) { latest_known_healthy_timestamp_ = ts; }

   private:
    void StartHealthCheck();

    std::weak_ptr<GcsHealthCheckManager> manager_;

    NodeID node_id_;

    // Timestamp for latest known status when node is healthy.
    absl::Time latest_known_healthy_timestamp_ = absl::InfinitePast();

    // Whether the health check has stopped.
    bool stopped_ = false;

    /// gRPC related fields
    std::unique_ptr<::grpc::health::v1::Health::Stub> stub_;

    ::grpc::health::v1::HealthCheckRequest request_;

    /// The timer is used to do async wait before the next try.
    Timer timer_;

    /// The remaining check left. If it reaches 0, the node will be marked as dead.
    int64_t health_check_remaining_;
  };

  /// The main service. All method needs to run on this thread.
  instrumented_io_context &io_service_;

  /// Callback when the node failed.
  std::function<void(const NodeID &)> on_node_death_callback_;

  /// The context of the health check for each nodes.
  /// Only living nodes are bookkept, while failed one will be removed.
  absl::flat_hash_map<NodeID, HealthCheckContext *> health_check_contexts_;

  /// Checker to make sure there's no concurrent access for node addition and removal.
  const ThreadChecker thread_checker_;

  /// The delay for the first health check request.
  const int64_t initial_delay_ms_;
  /// Timeout for each health check request.
  const int64_t timeout_ms_;
  /// Intervals between two health check.
  const int64_t period_ms_;
  /// The number of failures before the node is considered as dead.
  const int64_t failure_threshold_;
};

}  // namespace ray::gcs
